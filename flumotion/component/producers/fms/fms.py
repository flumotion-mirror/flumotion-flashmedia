# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4

# Flumotion - a streaming media server
# Copyright (C) 2004,2005,2006,2007,2008,2009 Fluendo, S.L.
# Copyright (C) 2010,2011 Flumotion Services, S.A.
# All rights reserved.
#
# This file may be distributed and/or modified under the terms of
# the GNU Lesser General Public License version 2.1 as published by
# the Free Software Foundation.
# This file is distributed without any warranty; without even the implied
# warranty of merchantability or fitness for a particular purpose.
# See "LICENSE.LGPL" in the source distribution for more information.
#
# Headers in this file shall remain intact.

import gst
import time
import socket
from StringIO import StringIO

from flvlib import tags
from flvlib.constants import *

from rtmpy import server, exc

from twisted.internet import reactor, error, defer
from twisted.internet.task import LoopingCall
from twisted.python import util

from flumotion.common import log, errors
from flumotion.common.i18n import gettexter, N_
from flumotion.common import messages
from flumotion.component import feedcomponent
from flumotion.component.component import moods

from flumotion.component.producers.fms import live


T_ = gettexter('flumotion-flashmedia')

sound_format_has_headers = {SOUND_FORMAT_AAC: True}
codec_id_has_headers = {CODEC_ID_H264: True}

UI_UPDATE_THROTTLE_PERIOD = 5.0
UI_MAX_ACTIONS_KEPT = 100


#TODO: Factor out the application logic from the part that actually handles the
# flv chunks and does all the processing. Right now the application is at the
# same time the subscriber.

STARTCODE = "\x00\x00\x00\x01"

NAL_UNIT_TYPE_SPS = 7
NAL_UNIT_TYPE_PPS = 8
NAL_UNIT_TYPE_AUD = 9


class FMSApplication(server.Application, log.Loggable):

    logCategory = 'fms-app'
    client = live.Client

    MAX_TAG_BACKLOG = 15

    def __init__(self, component, streamName):
        server.Application.__init__(self)
        self._component = component

        self._streamName = streamName
        self._stream = None
        self._client = None

        self._published = False
        self._started = False
        self._changed = False
        self._failed = False

        self._syncTimestamp = -1
        self._syncOffset = -1
        self._totalTime = 0

        self._creationdate = None
        self._metadata = None

        self._audioinfo = None
        self._videoinfo = None

        self._needAudioHeader = False
        self._needVideoHeader = False

        self._firstAudioReceived = False
        self._firstVideoReceived = False

        self._firstAudioSent = False
        self._firstVideoSent = False

        self._gotAudioHeader = False
        self._gotVideoHeader = False

        self._videoEnabled = False
        self._audioEnabled = False

        self._backupVideoHeader = None
        self._backupAudioHeader = None

        self._backlog = []
        self._headers = []

        self._sps_len = 0
        self._pps_len = 0

    def onPublish(self, client, stream):
        peer = client.nc.transport.getPeer()
        self.info("Client %s:%d publishing stream %s", peer.host, peer.port,
            stream.name)
        # If we failed we just refuse everything
        if self._failed:
            return False

        self._client = client
        self._stream = stream
        self.addSubscriber(stream, self)
        self.streamPublished()

    def publishStream(self, client, stream, name, type_):
        """ Called while publishing the stream. Here we can decide
        wether the stream can be published or not for that client.
        """
        peer = client.nc.transport.getPeer()
        if name != self._streamName:
            self.debug("Stream %s refused: stream name should be %s",
                       name, self._streamName)
            self._component.new_client_event("badname", peer.host, peer.port,
                                             name)
            raise exc.BadNameError('%s is not a valid name' % (name, ))

        # Check if we have already a client publishing
        if self._client and client != self._client:
            self.debug("We have another client publishing %s: Checking "
                       "its status...", name)
            if not self._component.starving:
                # If we are not starving (original client still streaming)
                # refuse the publish request
                self.debug("...and it is still publishing. Sorry, but we have "
                           "to refuse this request")
                self._component.new_client_event("ispublished", peer.host,
                                                 peer.port)
                raise exc.BadNameError('%s is already published!' % (name, ))
            else:
                self.debug("The other client is slacking or is lost: "
                           "disconnecting and unpublishing the old stream.")
                self.onDisconnect(self._client)
                try:
                    self.unpublishStream(name, self._stream)
                except exc.BadNameError:
                    self.debug("Stream %s already unpublished! Going ahead "
                               "with the publication", name)

        self.debug("And last! Your stream can be published.")
        return server.Application.publishStream(self, client, stream,
                                                name, type_)

    def unpublish(self):
        #TODO: Do wathever we need to do when the stream is unpublished
        self.streamUnpublished()

    def onConnect(self, client, **args):
        peer = client.nc.transport.getPeer()
        self.info("Client %s:%d connected", peer.host, peer.port)
        self._component.new_client_event("connected", peer.host, peer.port)
        return server.Application.onConnect(self, client)

    def onDisconnect(self, client):
        peer = client.nc.transport.getPeer()
        self.info("Client %s:%d disconnected", peer.host, peer.port)
        server.Application.onDisconnect(self, client)
        if client == self._client:
            self._client = None
        self._component.new_client_event("disconnected", peer.host, peer.port)

    def streamPublished(self):
        self.debug("Stream %s published", self._streamName)
        if self._published:
            self._internalError('Client tried to publish multiple times')
            return
        self._published = True

        peer = self._client.nc.transport.getPeer()
        self._component.new_client_event("published", peer.host, peer.port)

    def streamUnpublished(self):
        self.debug("Stream %s unpublished", self._streamName)
        if not self._published:
            self._internalError("Client tried to unpublish a "
                                "stream not yet published")
            return

        # Do we need this? the subscribers are removed just after the unpublish
        # is done.
        # self.removeSubscriber(self._stream, self)
        self._stream = None
        self._syncTimestamp = -1
        self._syncOffset = -1

        self._published = False

        if self._client:
            peer = self._client.nc.transport.getPeer()
            self._component.new_client_event("unpublished", peer.host,
                                             peer.port)
        else:
            self._component.new_client_event("unpublished", 'unknown',
                                             'unknown')

    def onMetaData(self, data):
        self.debug("Meta-data: %r, %s", data, type(data))
        self._component.clear_sizes()

        if not data:
            self.debug("We have been asked to clear metadata, we will do "
                       "it as soon as we get new metadata")
            return

        if not self._published:
            self._internalError('Meta-data received for an unpublished stream')
            return

        if self._started and self._metadata is None:
            # Metadata received too late
            self.debug("Dropping late meta-data")
            return

        if self._creationdate is None:
            self._creationdate = data.get("creationdate", None)
        # Force the creation date to the first one seen,
        # to be able to compare metadata.
        if self._creationdate:
            data["creationdate"] = self._creationdate

        if self._metadata and self._metadata != data:
            self.debug("RTMP stream meta-data changed.")
            self._clear()

        if self._metadata is None:
            self._metadata = data

        self._component.got_metadata(data.copy())

        # a framerate of 1000 sounds unlikely, but seems to happen a lot
        # when ffmpeg streams an .flv file.  Adjust it.
        if self._metadata and self._metadata.get('framerate', None) == 1000:
            self.warning(
                'Client claims framerate is 1000 fps.  Adjusting to 25 fps.')
            self._metadata['framerate'] = 25

        if self._started:
            self.debug("Dropping unchanged meta-data tag")
        else:
            bin = tags.create_script_tag('onMetaData', self._metadata)
            self._addHeader(bin)
            self._tryStarting()

    def audioDataReceived(self, data, time):
        self._component.update_sizes("audio", data, time)
        if not self._firstAudioReceived:
            self.info('Received first audio buffer with timestamp %d ms',
                time)
            self._firstAudioReceived = True

        if not self._published:
            self._internalError('Audio frame received for an '
                                'unpublished stream')
            return

        if self._started and not self._audioEnabled:
            self.log("Audio disabled, dropping audio tag")
            return

        tag = tags.AudioTag(None, StringIO(data))
        # flvlib AudioTag parse_tag_content() seek to the end of the tag
        # and for this need the tag size normally set when calling parse().
        # Set it to a dummy value just to prevent TypeError.
        tag.size = 0
        tag.parse_tag_content()

        info = {'format': tag.sound_format,
                'rate': tag.sound_rate,
                'size': tag.sound_size,
                'type': tag.sound_type}

        if self._audioinfo and self._audioinfo != info:
            self.debug("RTMP audio characteristics changed. %r", info)
            self._clear()

        if self._audioinfo is None:
            self._audioinfo = info
            self._component.got_audio_info(info)
            nh = sound_format_has_headers.get(tag.sound_format, False)
            if nh:
                self.debug("Audio stream need sequence header")
            self._needAudioHeader = nh
            self._gotAudioHeader = False

        fixedTime = self._fixeTimestamp(time)
        flvTag = tags.create_flv_tag(TAG_TYPE_AUDIO, data, fixedTime)

        if tag.aac_packet_type == AAC_PACKET_TYPE_SEQUENCE_HEADER:
            assert self._needAudioHeader, "Audio header not expected"
            if self._gotAudioHeader:
                # FMLE might send the sequence header before the new metadata
                # which screws us up. We keep the tag instead of dropping it
                # so we can send it latter when the changes are detected.
                self._backupAudioHeader = flvTag
                self.debug("Keeping audio sequence header, just in case the "
                           "new metadata didn't come yet")
                return
            else:
                self.debug("Audio stream sequence header received")
                self._addHeader(flvTag)
                self._gotAudioHeader = True
                self._tryStarting()
                return
        elif self._needAudioHeader and self._backupAudioHeader:
            # There have been changes and we are waiting for a header tag but
            # what we got is a normal audio tag. It came earlier than expected
            # before the changes could be detected so we're sending it now.
            self.debug("Sending earlier audio header")
            self._addHeader(self._backupAudioHeader)
            self._gotAudioHeader = True
            self._backupAudioHeader = None

        buffer = self._buildDataBuffer(fixedTime, flvTag)
        buffer.flag_set(gst.BUFFER_FLAG_DELTA_UNIT)

        if not self._firstAudioSent:
            self.info('Sending first audio buffer for time %d ms '
                'with adjusted time %d ms', time, fixedTime)
            self._firstAudioSent = True

        self._pushStreamBuffer(buffer)

    def videoDataReceived(self, data, time):
        self._component.update_sizes("video", data, time)
        if not self._firstVideoReceived:
            self.info('Received first video buffer with timestamp %d ms',
                time)
            self._firstVideoReceived = True

        if not self._published:
            self._internalError('Video frame received for an '
                                'unpublished stream')
            return

        if self._started and not self._videoEnabled:
            self.log("Video disabled, dropping video tag")
            return

        tag = tags.VideoTag(None, StringIO(data))
        # flvlib VideoTag parse_tag_content() seek to the end of the tag
        # and for this need the tag size normally set when calling parse().
        # Set it to a dummy value just to prevent TypeError.
        tag.size = 0
        tag.parse_tag_content()

        info = {'codec': tag.codec_id}

        if self._videoinfo and self._videoinfo != info:
            self.debug("RTMP video characteristics changed. %r", time)
            self._clear()

        if self._videoinfo is None:
            self._videoinfo = info
            self._component.got_video_info(info)
            nh = codec_id_has_headers.get(tag.codec_id, False)
            if nh:
                self.debug("Video stream need sequence header")
            self._needVideoHeader = nh
            self._gotVideoHeader = False

        self.buildAndPushVideoBuffer(data, time, tag)

    def _parseHeader(self, data):
        self._sps_len = (ord(data[11])<<8) | ord(data[12])
        pps_pos = 14 + self._sps_len
        self._pps_len = (ord(data[pps_pos])<<8) | ord(data[pps_pos+1])
        self.debug("Got SPS of %d bytes and PPS of %d bytes",
                   self._sps_len, self._pps_len)

    def _removeStarCodes(self, data):
        to_remove = 0
        remove_from = 0
        start = 5

        index = data.find(STARTCODE, start, 70)
        while index > 0:
            self.debug("Found a start code inside an AVC packet at %d", index)

            if remove_from == 0:
                remove_from = index
            to_remove += len(STARTCODE)

            nal_unit_type = ord(data[index+len(STARTCODE)]) & 0x0F
            if nal_unit_type == NAL_UNIT_TYPE_AUD:
                self.debug("Found Access unit delimitier in stream. "
                           "Dropping it")
                to_remove += 2
                start += len(STARTCODE) + 2
            if nal_unit_type == NAL_UNIT_TYPE_SPS:
                self.debug("Found SPS in stream. Dropping it")
                to_remove += self._sps_len
                start += len(STARTCODE) + self._sps_len
            elif nal_unit_type == NAL_UNIT_TYPE_PPS:
                self.debug("Found PPS in stream. Dropping it")
                to_remove += self._pps_len
                start += self._pps_len
            else:
                # In case of an unknown NAL_UNIT_TYPE
                return data
            index = data.find(STARTCODE, start, 70)

        if to_remove:
            to_remove += 3
            dataio = StringIO(data[:remove_from] +
                              data[remove_from+to_remove:])
            total_bytes = (ord(data[7])<<8) | ord(data[8])
            total_bytes -= to_remove
            dataio.seek(7)
            dataio.write(chr(total_bytes >> 8))
            dataio.write(chr(total_bytes & 0xFF))
            data = dataio.getvalue()
        return data

    def buildAndPushVideoBuffer(self, data, time, tag):
        if tag.h264_packet_type == H264_PACKET_TYPE_END_OF_SEQUENCE:
            # the timestamp of this buffer is not continious and is sent
            # after the reconnection with a weird timestamp. use the last
            # timestamp for it.
            fixedTime = self._fixeTimestamp(self._totalTime)
        else:
            fixedTime = self._fixeTimestamp(time)

        if not self._firstVideoSent:
            self.info('Sending first video buffer for time %d ms '
                'with adjusted time %d ms', time, fixedTime)
            self._firstVideoSent = True

        if tag.h264_packet_type is not None:
            data = self._removeStarCodes(data)

        flvTag = tags.create_flv_tag(TAG_TYPE_VIDEO, data, fixedTime)

        if tag.h264_packet_type == H264_PACKET_TYPE_SEQUENCE_HEADER:
            assert self._needVideoHeader, "Video header not expected"
            self._parseHeader(data)
            if self._gotVideoHeader:
                # FMLE might send the sequence header before the new metadata
                # which screws us up. We keep the tag instead of dropping it
                # so we can send it latter when the changes are detected.
                self._backupVideoHeader = flvTag
                self.debug("Keeping video sequence header, just in case the "
                           "new metadata didn't come yet")
                return
            else:
                self.debug("Video stream sequence header received")
                self._addHeader(flvTag)
                self._gotVideoHeader = True
                self._tryStarting()
                return
        elif self._needVideoHeader and self._backupVideoHeader:
            # There have been changes and we are waiting for a header tag but
            # what we got is a normal video tag. It came earlier than expected
            # before the changes could be detected so we're sending it now.
            self.debug("Sending earlier video header")
            self._addHeader(self._backupVideoHeader)
            self._gotVideoHeader = True
            self._backupVideoHeader = None

        buffer = self._buildDataBuffer(fixedTime, flvTag)

        if tag.frame_type != FRAME_TYPE_KEYFRAME:
            buffer.flag_set(gst.BUFFER_FLAG_DELTA_UNIT)

        self._pushStreamBuffer(buffer)

    def _internalError(self, msg, debug=None):
        self._failed = True
        self._disconnect()
        self._component.appError(msg, debug=debug)

    def _disconnect(self):
        if self._client is not None:
            self.debug("Disconnecting from client")
            self.onDisconnect(self._client)

    def _fixeTimestamp(self, timestamp):
        """Given a timestamp finds out wether it is valid depending on the last
        timestamp sent. If the timestamp we receive is in the past it is fixed
        to be contiguous with the previous keeping the sync of audio and video.
        """
        if self._syncOffset == -1 or self._syncTimestamp == -1:
            self.debug("Adding new sync point at %s" % self._totalTime)
            # we want to re-timestamp from the timestamp of the last buffer
            # pushed, but not the same exact time. that's why we to the sync
            # point 40ms
            self._syncTimestamp = self._totalTime + 40
            self._syncOffset = timestamp

        fixedTimestamp = timestamp

        if timestamp < self._totalTime:
            fixedTimestamp = self._syncTimestamp - self._syncOffset + timestamp

        if fixedTimestamp < 0:
            self._internalError("Timestamp cannot be < 0")
            return

        self._totalTime = fixedTimestamp

        fixedTimestamp &= 0x7fffffff

        return fixedTimestamp

    def _addHeader(self, data):
        buffer = self._buildHeaderBuffer(data)
        self._headers.append(buffer)

    def _pushStreamBuffer(self, buffer):
        if self._started:
            self._component.pushStreamBuffer(buffer)
        else:
            self.log("Streaming not yet started, keeping buffer for later")
            self._backlog.append(buffer)
            self._tryStarting()

    def _tryStarting(self):
        assert not self._started, "Already started streaming"

        self.log("Trying to start streaming")

        if len(self._backlog) >= self.MAX_TAG_BACKLOG:
            self.debug("Buffer backlog full, force starting")
            self._start()
            return

        if not self._metadata:
            self.log("No meta-data received, deferring startup")
            return

        if not self._audioinfo:
            self.log("No audio tag received, deferring startup")
            return

        if self._needAudioHeader and not self._gotAudioHeader:
            self.log("No audio sequence header received, deferring startup")
            return

        if not self._videoinfo:
            self.log("No video tag received, deferring startup")
            return

        if self._needVideoHeader and not self._gotVideoHeader:
            self.log("No video sequence header received, deferring startup")
            return

        self._start()

    def _start(self):
        self.info("Starting feeding")

        hasVideo = True
        if not self._videoinfo:
            self.debug("No video tag received, video disabled")
            hasVideo = False
        else:
            if self._needVideoHeader and not self._gotVideoHeader:
                self.debug("No video sequence header received, video disabled")
                hasVideo = False

        hasAudio = True
        if not self._audioinfo:
            self.debug("No Audio tag received, audio disabled")
            hasAudio = False
        else:
            if self._needAudioHeader and not self._gotAudioHeader:
                self.debug("No Audio sequence header received, audio disabled")
                hasAudio = False

        self._videoEnabled = hasVideo
        self._audioEnabled = hasAudio

        header = tags.create_flv_header(hasAudio, hasVideo)
        buffer = self._buildHeaderBuffer(header)

        caps = gst.caps_from_string("video/x-flv")
        caps[0]['streamheader'] = (buffer, ) + tuple(self._headers)
        self._component.setStreamCaps(caps)

        if self._backlog:
            self.debug("Flushing backlog of %d buffers", len(self._backlog))
            for buffer in self._backlog:
                self._component.pushStreamBuffer(buffer)
            self._backlog = []

        self._started = True
        self._changed = False

    def _clear(self):
        if not self._started:
            self.log("Not clearing an stopped stream")
            return

        self.debug("Stopping streaming")

        self._started = False
        self._changed = True

        self._metadata = None

        self._audioinfo = None
        self._videoinfo = None

        self._needAudioHeader = False
        self._needVideoHeader = False

        self._gotAudioHeader = False
        self._gotVideoHeader = False

        self._videoEnabled = False
        self._audioEnabled = False
        self._syncTimestamp = -1
        self._syncOffset = -1

        self._headers = []

        self._sps_len = 0
        self._pps_len = 0

    def _buildHeaderBuffer(self, data, with_in_caps=True):
        buff = gst.Buffer(data)
        buff.timestamp = gst.CLOCK_TIME_NONE
        buff.duration = gst.CLOCK_TIME_NONE
        if with_in_caps:
            buff.flag_set(gst.BUFFER_FLAG_IN_CAPS)
        return buff

    def _buildDataBuffer(self, timestamp, data):
        buff = gst.Buffer(data)
        buff.timestamp = timestamp * gst.MSECOND
        buff.duration = gst.CLOCK_TIME_NONE
        return buff


class FlashMediaServer(feedcomponent.ParseLaunchComponent):

    logCategory = 'fms'
    DEFAULT_PORT = 1935
    DEFAULT_MOUNT = '/live/stream.flv'

    def init(self):
        self._port = None
        self._appName = None
        self._streamName = None
        self._source = None
        self.starving = False

        self._sizes = {'audio': util.OrderedDict(),
                       'video': util.OrderedDict()}

        self._update_task = None
        self._posted_message = None

        self.uiState.addDictKey('metadata')
        self.uiState.addDictKey('codec-info')
        self.uiState.addKey('upload-bw', {"video": 0, "audio": 0})
        self.uiState.addKey('total-connections', 0)
        self.uiState.addKey('last-connect', 0)  # last client connect, epoch
        self.uiState.addListKey('upload-fps', 0)
        self.uiState.addListKey('encoder-host', [])
        self.uiState.addListKey('client-events', [])

    def get_pipeline_string(self, properties):
        return 'appsrc name=source'

    def configure_pipeline(self, pipeline, properties):
        self._source = self.pipeline.get_by_name("source")
        self._pad_monitors.attach(self._source.get_pad('src'), 'fms-src')
        self._pad_monitors['fms-src'].addWatch(self._i_am_being_feed,
                                               self._i_am_starving)

    def check_properties(self, properties, addMessage):

        def postMountPointError():
            msg = ("Invalid mount point, it must be absolute and contains "
                   "at least two parts. For example: '/live/stream.flv'")
            self.warning(msg)
            m = messages.Error(T_(N_(msg)))
            addMessage(m)

        self._port = int(properties.get('port', self.DEFAULT_PORT))
        mountpoint = properties.get('mount-point', self.DEFAULT_MOUNT)
        parts = mountpoint.split('/', 2)
        if len(parts) != 3:
            postMountPointError()
            return
        nothing, aname, sname = parts
        if nothing != '':
            postMountPointError()
            return
        self._appName = aname
        self._streamName = sname

    def got_metadata(self, metadata):
        self.uiState.set('metadata', metadata)

    def got_audio_info(self, info):
        self.uiState.setitem('codec-info', 'audiocodec',
                             sound_format_to_string[info['format']])
        self.uiState.setitem('codec-info', 'audiorate',
                             sound_rate_to_string[info['rate']])
        self.uiState.setitem('codec-info', 'audiodepth',
                             sound_size_to_string[info['size']][3:])
        self.uiState.setitem('codec-info', 'audiochannels',
                             sound_type_to_string[info['type']][3:])

    def got_video_info(self, info):
        self.uiState.setitem('codec-info', 'videocodec',
                             codec_id_to_string[info['codec']])

    def _calculate_bandwidth(self, times, sizes):
        size = sum(sizes)
        elapsed_time = (times[-1] - times[0]) / 1000.0
        if not elapsed_time:
            return 0.0

        return 8 * float(size) / elapsed_time

    def _calculate_fps(self, times):
        frames = len(times)
        elapsed_time = (times[-1] - times[0]) / 1000.0
        if not elapsed_time:
            return 0.0

        return float(frames) / elapsed_time

    def _update_ui_state(self):
        bandwidths = self.uiState.get('upload-bw', {})
        if self._sizes['video']:
            video_bps = self._calculate_bandwidth(
                self._sizes['video'].keys(), self._sizes['video'].values())
            bandwidths.update({"video": video_bps})

            video_fps = self._calculate_fps(self._sizes['video'].keys())
            self.uiState.set('upload-fps', video_fps)

        if self._sizes['audio']:
            audio_bps = self._calculate_bandwidth(
                self._sizes['audio'].keys(), self._sizes['audio'].values())
            bandwidths.update({"audio": audio_bps})

        msg = None
        if not sum(bandwidths.values()):
            if self._posted_message != 'data':
                self._posted_message= 'data'
                msg = messages.Warning(T_(N_("Client isn't pushing data!!")))
        elif not bandwidths.get('video', 0):
            if self._posted_message != 'video':
                self._posted_message = 'video'
                msg = messages.Warning(T_(N_("Client isn't pushing video!!")))
        elif not bandwidths.get('audio', 0):
            if self._posted_message != 'audio':
                self._posted_message = 'audio'
                msg = messages.Warning(T_(N_("Client isn't pushing audio!!")))
        else:
            if self._posted_message:
                self._posted_message = ''
                self.removeMessage("input-status")

        if msg:
            msg.id = "input-status"
            self.addMessage(msg)

        self.uiState.set('upload-bw', bandwidths)

    def clear_sizes(self):
        self._sizes['video'] = util.OrderedDict()
        self._sizes['audio'] = util.OrderedDict()

    def update_sizes(self, mtype, data, time):
        sizes = self._sizes[mtype]
        sizes[time] = len(data)
        times = sizes.keys()

        if len(times) < 2:
            return

        if times[-1] - times[0] > UI_UPDATE_THROTTLE_PERIOD * 1000:
            del sizes[times[0]]
            if not self._update_task:
                self._update_task = LoopingCall(self._update_ui_state)
                self._update_task.start(UI_UPDATE_THROTTLE_PERIOD, now=True)

    def new_client_event(self, event, host, port, name=None):
        try:
            hostname = socket.gethostbyaddr(host)[0]
        except:
            hostname = ''

        msg = None

        if event == 'connected':
            self.uiState.set('total-connections',
                             self.uiState.get('total-connections', 0) + 1)
            self.uiState.set('last-connect', time.time())

            msg = messages.Info(T_(N_(
                    "New client connected from %s:%s"),
                    hostname and hostname or host, port))
        elif event == 'published':
            self.uiState.set('encoder-host', [host, hostname, port])

            msg = messages.Info(T_(N_(
                    "Client publishing from %s:%s"),
                    hostname and hostname or host, port))
        elif event == 'unpublished':
            if self._update_task:
                self._update_task.stop()
                self._update_task = None
            self.uiState.set('upload-bw', {"video": 0, "audio": 0})
            self.uiState.set('upload-fps', 0.0)

            msg = messages.Warning(T_(N_(
                    "Client from %s:%s been unpublished"),
                    hostname and hostname or host, port))
        elif event == 'disconnect':
            msg = messages.Warning(T_(N_(
                    "Client from %s:%s has been disconnected"),
                    hostname and hostname or host, port))
        elif event == 'badname':
            msg = messages.Warning(T_(N_(
                "Client from %s:%s is trying to publish "
                "to the wrong path (%s)"),
                hostname and hostname or host, port, name or ""))
        elif event == 'ispublished':
            msg = messages.Warning(T_(N_(
                    "A new client from %s:%s is trying to publish, "
                    "but we already have a publisher"),
                    hostname and hostname or host, port))

        if msg:
            msg.id = "encoder-event"
            self.addMessage(msg)

        event = {"ip": host, "hostname": hostname, "port": port,
                  "event": event, "timestamp": time.time()}

        events = self.uiState.get('client-events')

        if len(events) >= UI_MAX_ACTIONS_KEPT:
            self.uiState.remove('client-events', events[0])
        self.uiState.append('client-events', event)

    def do_setup(self):
        app = FMSApplication(self, self._streamName)
        factory = server.ServerFactory({self._appName: app})

        try:
            self.debug('Listening on TCP port %d' % self._port)
            reactor.listenTCP(self._port, factory)
        except error.CannotListenError:
            t = 'TCP port %d is not available.' % self._port
            self.warning(t)
            m = messages.Error(T_(N_(
                "Network error: TCP port %d is not available."), self._port))
            self.addMessage(m)
            self.setMood(moods.sad)
            return defer.fail(errors.ComponentStartHandledError(t))

    def appError(self, msg, debug=None):
        self.warning(msg)
        self.addMessage(messages.Error(T_(N_(msg)), debug=debug))

    def setStreamCaps(self, caps):
        self._source.get_pad('src').set_caps(caps)

    def pushStreamBuffer(self, buffer):
        self._source.emit('push-buffer', buffer)

    def _i_am_starving(self, name):
        if not self.starving:
            self.debug("Not receiving RTMP data from encoder.")
            self.starving = True

    def _i_am_being_feed(self, name):
        if self.starving:
            self.debug("RTMP data received, we are not starving anymore")
            self.starving = False
