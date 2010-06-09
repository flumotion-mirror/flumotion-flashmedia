# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4
#
# Flumotion - a streaming media server
# Copyright (C) 2007,2008 Fluendo, S.L. (www.fluendo.com).
# All rights reserved.

# Licensees having purchased or holding a valid Flumotion Advanced
# Streaming Server license may use this file in accordance with the
# Flumotion Advanced Streaming Server Commercial License Agreement.
# See "LICENSE.Flumotion" in the source distribution for more information.

# Headers in this file shall remain intact.

import gst
import struct
from StringIO import StringIO

from flvlib import tags
from flvlib.constants import *

from rtmpy import server

from twisted.internet import reactor, error, defer

from flumotion.common import log, errors
from flumotion.common.i18n import gettexter, N_
from flumotion.common.messages import Error
from flumotion.component import feedcomponent
from flumotion.component.component import moods

from flumotion.component.producers.fms import live


T_ = gettexter('flumotion-flashmedia')

sound_format_has_headers = {SOUND_FORMAT_AAC: True}
codec_id_has_headers = {CODEC_ID_H264: True}


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

        # FMLE is pushing one video buffer and after that lots of audio buffers
        # before the next video. This is causing synchronization problems.
        self._synced = False
        self._previousVideoTimestamp = None
        self._videoBufferDuration = None

        self._published = False
        self._started = False
        self._changed = False
        self._failed = False
        self._publishing = False

        self._lastTimestamp = 0
        self._totalTime = 0

        self._creationdate = None
        self._metadata = None

        self._audioinfo = None
        self._videoinfo = None

        self._needAudioHeader = False
        self._needVideoHeader = False

        self._gotAudioHeader = False
        self._gotVideoHeader = False

        self._videoEnabled = False
        self._audioEnabled = False

        self._backupVideoHeader = None
        self._backupAudioHeader = None

        self._backlog = []
        self._headers = []


    def prePublish(self, client, stream):
        self._synced = False
        self._previousVideoTimestamp = None
        self._videoBufferDuration = None
        if stream.publisher:
            if self._component.starving and not self._publishing:
                try:
                    stream.publisher.disconnect()
                except AttributeError, e:
                    self.debug("Publisher already disconnected. Caught "
                               "exception from Client when trying to "
                               "disconnect: %s", log.getExceptionMessage(e))
                stream.removePublisher(self._client)
            else:
                return False
        self._publishing = True
        return self.onPublish(client, stream)

    def onPublish(self, client, stream):
        self.debug("Client %r publishing stream %s", client, stream.name)

        # If we failed we just refuse everything
        if self._failed:
            self._publishing = False
            return False

        if stream.name != self._streamName:
            self.debug("Stream %s refused", stream.name)
            self._publishing = False
            return False

        self._client = client
        self._stream = stream
        stream.addSubscriber(self)
        self._publishing = False

    def onDisconnect(self, client):
        server.Application.onDisconnect(self, client)
        if client == self._client:
            self._client = None

    def streamPublished(self):
        self.debug("Stream %s published", self._streamName)

        if self._published:
            self._internalError('Client try to publish multiple times')
            return

        self._published = True

    def streamUnpublished(self):
        self.debug("Stream %s unpublished", self._streamName)

        if not self._published:
            self._internalError("Client try to unpublish a "
                                "stream not yet published")
            return

        self._stream.removeSubscriber(self)
        self._stream = None
        self._lastTimestamp = 0

        self._published = False

    def onMetaData(self, data):
        self.debug("Meta-data: %r", data)

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

        self._videoBufferDuration = 1000 / self._metadata.get('framerate', 10)
        self._videoBufferDuration += self._videoBufferDuration * 0.10
        self.log('Video buffer duration: %d', self._videoBufferDuration)

        if self._started:
            self.debug("Dropping unchanged meta-data tag")
        else:
            bin = tags.create_script_tag('onMetaData', self._metadata)
            self._addHeader(bin)
            self._tryStarting()

    def audioDataReceived(self, data, time):
        self.log("Audio frame: %d ms, %d bytes", time, len(data))

        if not self._published:
            self._internalError('Audio frame received for an unpublished stream')
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
            self.debug("RTMP audio characteristics changed.")
            self._clear()

        if self._audioinfo is None:
            self._audioinfo = info
            nh = sound_format_has_headers.get(tag.sound_format, False)
            if nh:
                self.debug("Audio stream need sequence header")
            self._needAudioHeader = nh
            self._gotAudioHeader = False

        fixedTime = self._fixeTimestamp(time)
        flvTag = tags.create_flv_tag(TAG_TYPE_AUDIO, data, fixedTime)

        if tag.aac_packet_type == AAC_PACKET_TYPE_SEQUENCE_HEADER:
            assert self._needAudioHeader, "Audio header not expected"
            if self._gotAudioHeader and not self._synced:
                # FMLE might send the sequence header before the new metadata
                # which screws us up. We keep the tag instead of dropping it
                # so we can send it latter when the changes are detected.
                self._backupAudioHeader = flvTag
                self.debug("Keeping audio sequence header just in case the "
                           "new metadata didn't come yet")
                return
            else:
                self.debug("Audio stream sequence header received")
                self._addHeader(flvTag)
                self._gotAudioHeader = True
                self._tryStarting()
                return
        elif not self._synced:
            self.log('Discarting audio buffer. Video not synced yet')
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

        self._pushStreamBuffer(buffer)

    def videoDataReceived(self, data, time):
        self.log("Video frame: %d ms, %d bytes", time, len(data))

        if not self._published:
            self._internalError('Video frame received for an unpublished stream')
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
            nh = codec_id_has_headers.get(tag.codec_id, False)
            if nh:
                self.debug("Video stream need sequence header")
            self._needVideoHeader = nh
            self._gotVideoHeader = False

        if not self._synced:
            if tag.h264_packet_type == H264_PACKET_TYPE_SEQUENCE_HEADER:
                fixedTime = self._fixeTimestamp(time)
                flvTag = tags.create_flv_tag(TAG_TYPE_VIDEO, data, fixedTime)
                self._backupVideoHeader = flvTag
                return
            # Looking for a consecutive keyframe before pushing audio buffers
            if tag.frame_type != FRAME_TYPE_KEYFRAME:
                self._previousVideoTimestamp = time
                return

            if not self._previousVideoTimestamp:
                self._previousVideoTimestamp = time
                return

            if time - self._previousVideoTimestamp > self._videoBufferDuration:
                self.log("Got keyframe not in sync with last frame")
                self._previousVideoTimestamp = time
                return
            else:
                self.log("Got keyframe in sync with last frame")
                self._synced = True

        self.log("Got video buffer with timestamp %d", time)
        self.buildAndPushVideoBuffer(data, time, tag)

    def buildAndPushVideoBuffer(self, data, time, tag):
        fixedTime = self._fixeTimestamp(time)
        flvTag = tags.create_flv_tag(TAG_TYPE_VIDEO, data, fixedTime)

        if tag.h264_packet_type == H264_PACKET_TYPE_SEQUENCE_HEADER:
            assert self._needVideoHeader, "Video header not expected"
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
            self._client.disconnect()

    def _fixeTimestamp(self, timestamp):
        """Given a timestamp finds out wether it is valid depending on the last
        timestamp sent. If the timestamp we receive is in the past it is fixed
        to be contiguous with the previous.
        """
        fixedTimestamp = timestamp

        if timestamp < self._totalTime:
            fixedTimestamp = self._totalTime + timestamp - self._lastTimestamp
            self._lastTimestamp = timestamp

        if fixedTimestamp < 0:
            self._internalError("Timestamp cannot be < 0")
            return

        self._totalTime = fixedTimestamp

        fixedTimestamp &= 0x7fffffff

        return fixedTimestamp

    def _addHeader(self, data):
        buffer = self._buildHeaderBuffer(data)
        self._headers.append(buffer)
        if self._changed:
            # When changes arrive we want to send the new headers without the
            # IN_CAPS flag so the buffers are not dropped because of the change
            # This way the stream is valid and playable for any flash player.
            self._pushStreamBuffer(self._buildHeaderBuffer(data, False))

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
        self.debug("Starting streaming")

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
        caps[0]['streamheader'] = (buffer,) + tuple(self._headers)
        self._component.setStreamCaps(caps)
        
        self.debug("RESET: send event downstream")
        self._component.sendEvent(gst.event_new_custom(gst.EVENT_CUSTOM_DOWNSTREAM,
                                                       gst.Structure('flumotion-reset')))

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
        self._lastTimestamp = 0

        self._headers = []

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
    MONITORING_FREQUENCY = 5.0

    def init(self):
        self._port = None
        self._appName = None
        self._streamName = None
        self._source = None

        # For monitorization
        self._bufferCount = 0
        self._monitoringCall = None

        self.starving = False

    def get_pipeline_string(self, properties):
        return 'appsrc name=source caps=video/x-flv'

    def configure_pipeline(self, pipeline, properties):
        self._source = self.pipeline.get_by_name("source")

    def check_properties(self, properties, addMessage):

        def postMountPointError():
            msg = ("Invalid mount point, it must be absolute and contains "
                   "at least two parts. For example: '/live/stream.flv'")
            self.warning(msg)
            m = Error(T_(N_(msg)))
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

    def do_setup(self):
        app = FMSApplication(self, self._streamName)
        factory = server.ServerFactory({self._appName: app})

        self._scheduleMonitoring()

        try:
            self.debug('Listening on %d' % self._port)
            reactor.listenTCP(self._port, factory)
        except error.CannotListenError:
            t = 'Port %d is not available.' % self._port
            self.warning(t)
            m = Error(T_(N_(
                "Network error: TCP port %d is not available."), self._port))
            self.addMessage(m)
            self.setMood(moods.sad)
            return defer.fail(errors.ComponentStartHandledError(t))

    def do_stop(self):
        self._cancelMonitoring()

    def appError(self, msg, debug=None):
        self.warning(msg)
        self.addMessage(Error(T_(N_(msg)), debug=debug))

    def setStreamCaps(self, caps):
        self._source.props.caps = caps

    def pushStreamBuffer(self, buffer):
        self._bufferCount += 1
        self._source.emit('push-buffer', buffer)

    def sendEvent(self, event):
        self.log('Sending flumotion-reset event downstream')
        self._source.get_pad('src').get_peer().send_event(event)

    def _scheduleMonitoring(self):
        dc = reactor.callLater(self.MONITORING_FREQUENCY, self._doMonitoring)
        self._monitoringCall = dc

    def _cancelMonitoring(self):
        if self._monitoringCall is not None:
            self._monitoringCall.cancel()
            self._monitoringCall = None

    def _doMonitoring(self):
        if self._bufferCount == 0:
            if not self.starving:
                self.debug("No RTMP data received since %0.2f seconds, "
                           "we are starving", self.MONITORING_FREQUENCY)
                self.setMood(moods.hungry)
                self.starving = True
        else:
            if self.starving:
                self.debug("RTMP data received, we are not starving anymore")
                self.setMood(moods.happy)
                self.starving = False
            self._bufferCount = 0
        self._scheduleMonitoring()
