# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4
#
# Flumotion - a streaming media server
# Copyright (C) 2004,2005,2006,2007,2008 Fluendo, S.L. (www.fluendo.com).
# All rights reserved.

# This file may be distributed and/or modified under the terms of
# the GNU General Public License version 2 as published by
# the Free Software Foundation.
# This file is distributed without any warranty; without even the implied
# warranty of merchantability or fitness for a particular purpose.
# See "LICENSE.GPL" in the source distribution for more information.

# Licensees having purchased or holding a valid Flumotion Advanced
# Streaming Server license may use this file in accordance with the
# Flumotion Advanced Streaming Server Commercial License Agreement.
# See "LICENSE.Flumotion" in the source distribution for more information.

# Headers in this file shall remain intact.

import gettext
import os
import time
import gtk
import pango

from kiwi.ui.objectlist import Column
from kiwi.python import Settable
from twisted.internet import task

from flumotion.common.format import formatStorage, formatTime
from flumotion.component.base.admin_gtk import BaseAdminGtk
from flumotion.component.base.baseadminnode import BaseAdminGtkNode

__version__ = "$Rev$"
_ = gettext.gettext


class MediaAdminGtkNode(BaseAdminGtkNode):
    gladeFile = os.path.join('flumotion', 'component',
                             'producers', 'fms', 'fms.glade')

    def haveWidgetTree(self):
        self.widget = self.wtree.get_widget('media_vbox')

        # Video
        self._label_video_device = self.wtree.get_widget('video_device')
        self._label_video_codec = self.wtree.get_widget('video_codec')
        self._label_video_framerate = self.wtree.get_widget('video_framerate')
        self._label_video_keyframe = self.wtree.get_widget('video_keyframe_dist')
        self._label_video_br = self.wtree.get_widget('video_bitrate')
        self._label_video_size = self.wtree.get_widget('video_size')

        # Audio
        self._label_audio_device = self.wtree.get_widget('audio_device')
        self._label_audio_codec = self.wtree.get_widget('audio_codec')
        self._label_audio_rate = self.wtree.get_widget('audio_samplerate')
        self._label_audio_br = self.wtree.get_widget('audio_bitrate')
        self._label_audio_channels = self.wtree.get_widget('audio_channels')
        self._label_audio_depth = self.wtree.get_widget('audio_depth')

        self.widget.show_all()
        return self.widget

    def setUIState(self, state):
        BaseAdminGtkNode.setUIState(self, state)
        if not self.widget:
            return

        self.haveMetadata(state.get('metadata', {}))
        self.haveCodecInfo(state.get('codec-info', {}))
    
    def stateSet(self, state, key, value):
        if key == "codec-info":
            self.haveCodecInfo(value)
        elif key == "metadata":
            self.haveMetadata(value)

    def stateSetitem(self, state, key, subkey, value):
        if key != 'codec-info':
            return

        if subkey == 'audiocodec':
            self._label_audio_codec.set_text(value)
        elif subkey == 'videocodec':
            self._label_video_codec.set_text(value)
        elif subkey == 'audiorate':
            self._label_audio_rate.set_text(value)
        elif subkey == 'audiochannels':
            self._label_audio_channels.set_text(value)
        elif subkey == 'audiodepth':
            self._label_audio_depth.set_text(value)

    def haveMetadata(self, metadata):
        self._clean_metadata_labels()

        # Video
        device = metadata.get('videodevice', '')
        if device:
            self._label_video_device.set_text(device)

        framerate = metadata.get('framerate', 0)
        if framerate:
            self._label_video_framerate.set_text("%d fps" % framerate)

        bitrate = metadata.get('videodatarate', 0)
        if bitrate:
            self._label_video_br.set_text(formatStorage(bitrate*1000) + _("bit/s"))
        
        keyframe_dist = metadata.get('videokeyframe_frequency', 0)
        if keyframe_dist:
            self._label_video_keyframe.set_text("%d s" % keyframe_dist)

        width = metadata.get('width', 0)
        height = metadata.get('height', 0)
        if width:
            if height:
                self._label_video_size.set_text("%d x %d" % (width, height))
            else:
                self._label_video_size.set_text("%d x Unknown" % width)
        elif height:
            self._label_video_size.set_text("Unknown x %d" % height)

        # Audio
        device = metadata.get('audiodevice', 'Unknown')
        self._label_audio_device.set_text(device)

        bitrate = metadata.get('audiodatarate', 0)
        if bitrate:
            self._label_audio_br.set_text(formatStorage(bitrate*1000) +
                                          _("bit/s"))

    def haveCodecInfo(self, codec_info):
        self._clean_codec_labels()
        for subkey in codec_info.keys():
            self.stateSetitem(self.state, 'codec-info', subkey,
                              codec_info[subkey])


    def _clean_codec_labels(self):
        self._label_video_codec.set_text('Unknown')
        self._label_audio_codec.set_text('Unknown')
        self._label_audio_rate.set_text('Unknown')
        self._label_audio_channels.set_text('Unknown')
        self._label_audio_depth.set_text('Unknown')


    def _clean_metadata_labels(self):
        # Video
        self._label_video_device.set_text('Unknown')
        self._label_video_framerate.set_text('Unknown')
        self._label_video_keyframe.set_text('Unknown')
        self._label_video_br.set_text('Unknown')
        self._label_video_size.set_text('Unknown')

        # Audio
        self._label_audio_device.set_text('Unknown')
        self._label_audio_br.set_text('Unknown')


class ConnectionsAdminGtkNode(BaseAdminGtkNode):
    gladeFile = os.path.join('flumotion', 'component',
                             'producers', 'fms', 'fms.glade')

    uiStateHandlers = None
    widgets = None
    timer_counter = None

    def getWidget(self, name):
        if self.widgets is None:
            self.widgets = {}

        w = self.wtree.get_widget(name)
        if not w:
            raise KeyError("No widget %s found" % name)
        self.widgets[name] = w
        return w

    def haveWidgetTree(self):
        self.widget = self.wtree.get_widget('stats_vbox')
        properties = self.state.get('config')['properties']

        for name in [
            'label-type',
            'label-total-connections',
            'label-connection-description',
            'label-connection-time',
            'label-bandwidth-description',
            'label-bandwidth',
            'label-disconnection-description',
            'label-disconnection-time',
            'label-encoder-ip',
            'label-encoder-port',
            'label-encoder-mountpoint',
            'debug-connections-log',
            'connections',
            ]:
            self.getWidget(name)

        self.widgets['label-encoder-mountpoint'].set_text(properties.get('mount-point',
                                                                         '/live/stream.flv'))
        self.widgets['connections'].set_columns(
            [Column("ip", title=_("Encoder"), searchable=True),
             Column("timestamp", title=_("Timestamp"), sorted=True,
                    ellipsize=pango.ELLIPSIZE_START, expand=True,
                    order=gtk.SORT_DESCENDING),
             Column("event", title=_("Event")),
            ])

        self.widget.show_all()
        return self.widget
    
    def setUIState(self, state):
        BaseAdminGtkNode.setUIState(self, state)
        if not self.widget:
            return

        if not self.uiStateHandlers:
            self.uiStateHandlers = {
                'upload-bw':         self.uploadBandwidthSet,
                'total-connections': self.totalConnectionsSet,
                'encoder-host':      self.encoderHostSet,
                'last-connect':      self.lastConnectSet,
                'last-disconnect':   self.lastDisconnectSet,
                'client-events':    self.clientEventsSet,
            }

        for k, handler in self.uiStateHandlers.items():
            handler(state.get(k))

    def stateSet(self, state, key, value):
        handler = self.uiStateHandlers.get(key, None)
        if handler:
            handler(value)

    def stateAppend(self, state, key, value):
        handler = self.uiStateHandlers.get(key, None)
        if handler:
            handler(value)

    def uploadBandwidthSet(self, bw):
        if len(bw) == 1:
            self.widgets['label-bandwidth'].set_text(formatStorage(bw[0]) + _("bit/s"))
        elif len(bw) == 2:
            self.widgets['label-bandwidth'].set_text(
                "Video, %s\nAudio, %s\nTotal,  %s" %(
                    formatStorage(bw[0]) + _("bit/s"),
                    formatStorage(bw[1]) + _("bit/s"),
                    formatStorage(sum(bw)) + _("bit/s")))

    def totalConnectionsSet(self, count):
        self.widgets['label-total-connections'].set_text("%d" % count)

    def encoderHostSet(self, host):
        if len(host) == 2:
            self.widgets['label-encoder-ip'].set_text("%s" % host[0])
            self.widgets['label-encoder-port'].set_text("%s" % host[1])

    def lastConnectSet(self, last):
        w = self.widgets
        if last:
            w['label-connection-description'].show()
            w['label-connection-time'].show()
            w['label-bandwidth-description'].show()
            w['label-bandwidth'].show()
            w['label-connection-time'].show()

            if self.timer_counter:
                self.timer_counter.stop()
            self.timer_counter = \
                    task.LoopingCall(self._set_connection_time,
                                     w['label-connection-time'], last)
            self.timer_counter.start(60)
        else:
            w['label-connection-description'].hide()
            w['label-connection-time'].hide()
            w['label-bandwidth-description'].hide()
            w['label-bandwidth'].hide()

    def lastDisconnectSet(self, last):
        w = self.widgets
        if last:
            w['label-disconnection-description'].show()
            w['label-disconnection-time'].show()
            if self.timer_counter:
                self.timer_counter.stop()
            self.timer_counter = \
                    task.LoopingCall(self._set_connection_time,
                                     w['label-disconnection-time'], last)
            self.timer_counter.start(60)
        else:
            w['label-disconnection-description'].hide()
            w['label-disconnection-time'].hide()

    def _set_connection_time(self, label, ctime):
        text = formatTime(time.time() - ctime)
        label.set_text(text)


    def clientEventsSet(self, events):
        if not isinstance(events, list):
            events = [events]

        for event in events:
            self.widgets['connections'].append(
                Settable(ip="%s:%s" % (event['ip'], event['port']),
                         timestamp=event['timestamp'],
                         event=event['event']))


class MetadataAdminGtkNode(BaseAdminGtkNode):
    gladeFile = os.path.join('flumotion', 'component',
                             'producers', 'fms', 'fms.glade')

    def haveWidgetTree(self):
        self.widget = self.wtree.get_widget('metadata_vbox')
        self.metadata = self.wtree.get_widget('metadata')
        import pango
        self.metadata.set_columns(
            [Column("key", title=_("Key"), searchable=True, expand=False),
             Column("value", title=_("Value"), ellipsize=pango.ELLIPSIZE_END, expand=True),
            ])
        self.widget.show_all()
        return self.widget

    def setUIState(self, state):
        BaseAdminGtkNode.setUIState(self, state)
        if not self.widget:
            return
        self.setMetadata(state.get('metadata'))
    
    def stateSet(self, state, key, value):
        if key != "metadata":
            return
        self.setMetadata(value)

    def setMetadata(self, metadata):
        self.metadata.clear()
        for name in metadata.keys():
            self.metadata.append(
                Settable(key=name, value=metadata[name]))

class FMSAdminGtk(BaseAdminGtk):

    def setup(self):
        self.nodes['Media'] = MediaAdminGtkNode(self.state, self.admin,
                                                _("Media Info"))
        self.nodes['Connections'] = ConnectionsAdminGtkNode(self.state, self.admin,
                                                            _("Connections"))
        self.nodes['Metadata'] = MetadataAdminGtkNode(self.state, self.admin,
                                                      _("Metadata"))
        return BaseAdminGtk.setup(self)

GUIClass = FMSAdminGtk
