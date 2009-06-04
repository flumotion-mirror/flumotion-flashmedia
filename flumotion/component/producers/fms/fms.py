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

        self._published = False
        self._started = False
        self._failed = False

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

        self._backlog = []
        self._headers = []

    def onPublish(self, client, stream):
        self.debug("Client %r publishing stream %s", client, stream.name)

        # If we failed we just refuse everything
        if self._failed:
            return False

        if stream.name != self._streamName:
            self.debug("Stream %s refused", stream.name)
            return False

        self._client = client
        self._stream = stream
        stream.addSubscriber(self)

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
        self._lastTimestamp = 0
        self._tagBuffer = []

    def streamUnpublished(self):
        self.debug("Stream %s unpublished", self._streamName)

        if not self._published:
            self._internalError("Client try to unpublish a "
                                "stream not yet published")
            return

        self._stream.removeSubscriber(self)
        self._stream = None

        self._published = False
        self._totalTime += self._lastTimestamp

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

        if not self._metadata:
            self._metadata = data
        else:
            if self._metadata != data:
                self._streamTraitsError("RTMP stream meta-data changed.",
                                        self._metadata, data)
                return

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

        if self._audioinfo is None:
            self._audioinfo = info
            nh = sound_format_has_headers.get(tag.sound_format, False)
            if nh:
                self.debug("Audio stream need sequence header")
            self._needAudioHeader = nh
            self._gotAudioHeader = False
        else:
            if self._audioinfo != info:
                expected = self._makeAudioInfoHumanReadable(self._audioinfo)
                got = self._makeAudioInfoHumanReadable(info)
                self._streamTraitsError("RTMP audio characteristics changed.",
                                        expected, got)
                return

        fixedTime = self._fixeTimestamp(time)
        flvTag = tags.create_flv_tag(TAG_TYPE_AUDIO, data, fixedTime)

        if tag.aac_packet_type == AAC_PACKET_TYPE_SEQUENCE_HEADER:
            assert self._needAudioHeader, "Audio header not expected"
            if self._gotAudioHeader:
                self.debug("Dropping audio sequence header")
                return
            else:
                self.debug("Audio stream sequence header received")
                self._addHeader(flvTag)
                self._gotAudioHeader = True
                self._tryStarting()
                return

        buffer = self._buildDataBuffer(fixedTime, flvTag)

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

        if self._videoinfo is None:
            self._videoinfo = info
            nh = codec_id_has_headers.get(tag.codec_id, False)
            if nh:
                self.debug("Video stream need sequence header")
            self._needVideoHeader = nh
            self._gotVideoHeader = False
        else:
            if self._videoinfo != info:
                expected = self._makeVideoInfoHumanReadable(self._videoinfo)
                got = self._makeVideoInfoHumanReadable(info)
                self._streamTraitsError("RTMP video characteristics changed.",
                                        expected, got)
                return

        fixedTime = self._fixeTimestamp(time)
        flvTag = tags.create_flv_tag(TAG_TYPE_VIDEO, data, fixedTime)

        if tag.h264_packet_type == H264_PACKET_TYPE_SEQUENCE_HEADER:
            assert self._needVideoHeader, "Video header not expected"
            if self._gotVideoHeader:
                self.debug("Dropping video sequence header")
                return
            else:
                self.debug("Video stream sequence header received")
                self._addHeader(flvTag)
                self._gotVideoHeader = True
                self._tryStarting()
                return

        buffer = self._buildDataBuffer(fixedTime, flvTag)

        if tag.frame_type != FRAME_TYPE_KEYFRAME:
            buffer.flag_set(gst.BUFFER_FLAG_DELTA_UNIT)

        self._pushStreamBuffer(buffer)

    def _internalError(self, msg, debug=None):
        self._failed = True
        self._disconnect()
        self._component.appError(msg, debug=debug)

    def _streamTraitsError(self, msg, expected, got):
        changes = []

        for k in expected:
            if k in got:
                if expected[k] != got[k]:
                    changes.append("%s changed from %r to %r"
                                   % (k, expected[k], got[k]))
            else:
                changes.append("%s removed (was %r)"
                               % (k, expected[k]))

        for k in got:
            if k not in expected:
                changes.append("%s added (with value %r)"
                               % (k, got[k]))

        self._internalError(msg, debug="\n".join(changes))

    def _disconnect(self):
        if self._client is not None:
            self.debug("Disconnecting from client")
            self._client.disconnect()

    def _makeAudioInfoHumanReadable(self, info):
        return {'sound_format': sound_format_to_string[info['format']],
                'sample_rate': sound_rate_to_string[info['rate']],
                'sample_size': sound_size_to_string[info['size']],
                'sound_type': sound_type_to_string[info['type']]}

    def _makeVideoInfoHumanReadable(self, info):
        return {'video_codec': codec_id_to_string[info['codec']]}

    def _fixeTimestamp(self, timestamp):
        if timestamp < self._lastTimestamp:
            self._internalError("Negative timestamp detected %d (last one: %d)"
                                % (timestamp, self._lastTimestamp))
            return

        fixedTimestamp = timestamp + self._totalTime

        if fixedTimestamp < 0:
            self._internalError("Timestamp cannot be < 0")
            return

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

        for buffer in self._backlog:
            self._component.pushStreamBuffer(buffer)
        self._backlog = None

        self._started = True

    def _buildHeaderBuffer(self, data):
        buff = gst.Buffer(data)
        buff.timestamp = gst.CLOCK_TIME_NONE
        buff.duration = gst.CLOCK_TIME_NONE
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
        self._starved = False

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
        server = live.Server({self._appName: app})

        self._scheduleMonitoring()

        try:
            self.debug('Listening on %d' % self._port)
            reactor.listenTCP(self._port, server)
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

    def _scheduleMonitoring(self):
        dc = reactor.callLater(self.MONITORING_FREQUENCY, self._doMonitoring)
        self._monitoringCall = dc

    def _cancelMonitoring(self):
        if self._monitoringCall is not None:
            self._monitoringCall.cancel()
            self._monitoringCall = None

    def _doMonitoring(self):
        if self._bufferCount == 0:
            if not self._starved:
                self.debug("No RTMP data received since %0.2f seconds, "
                           "we are starving", self.MONITORING_FREQUENCY)
                self.setMood(moods.hungry)
                self._starved = True
        else:
            if self._starved:
                self.debug("RTMP data received, we are not starving anymore")
                self.setMood(moods.happy)
                self._starved = False
            self._bufferCount = 0
        self._scheduleMonitoring()
