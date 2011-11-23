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

"""
A port of the main.asc from the live application included in FMS's sample
applications.
"""

import os.path

import pyamf.util

from rtmpy import server


class FLVWriter(object):
    """
    Writes a streamed FLV to a file object, supporting reconnection. It will
    write correct FLV headers, Tag headers and Tag tails but will not ensure
    that the body of the tags are in any way correct.

    @ivar fileHandle: The file object that will receive the encoded FLV data.
    @type fileHandle: L{file}
    @ivar bufferLimit: The number of bytes to internally buffer before
        writing to L{fileHandle}. Set to 0 to write to the fileHandle directly.
    @type bufferLimit: C{int}
    @ivar active: Whether this object is actively encoding FLV.
    @type active: C{bool}
    @ivar metaData: Containing an aggregate of all meta data assigned to the
        FLV.
    @type metaData: C{dict}
    @ivar header: The bytes used when writing the FLV header.
    @type header: C{bytes}
    @note: C{fileHandle.close} is never called.
    """

    SIGNATURE = 'FLV'
    VERSION = 1

    AUDIO_DATA = 0x08
    VIDEO_DATA = 0x09
    META_DATA = 0x12

    def __init__(self, fileHandle, limit=0):
        self.fileHandle = fileHandle
        self.buffer = pyamf.util.BufferedByteStream()
        self.bufferLimit = int(limit)

        self.totalTime = 0
        self.active = False
        self.headerWritten = False
        self.metaData = None
        self.header = None

    def _flush(self):
        """
        Only flush the buffer if the limit has been reached.

        @note: Private.
        """
        if len(self.buffer) < self.bufferLimit:
            return

        self.flush()

    def flush(self):
        """
        Write the contents of `buffer` to the file object.
        """
        if len(self.buffer) == 0:
            return

        self.fileHandle.write(self.buffer.getvalue())
        self.buffer.truncate()
        self.fileHandle.flush()

    def _writeHeader(self):
        """
        Write an FLV header to the stream
        """
        if self.headerWritten:
            raise RuntimeError('Attempted to write an FLV header when it has '
                'already been written')

        s = self.buffer.tell()

        # Signature
        self.buffer.write(FLVWriter.SIGNATURE)

        # Version
        self.buffer.write_uchar(FLVWriter.VERSION)

        # TypeFlagsReserved - force both audio and video
        self.buffer.write_uchar(5)

        # DataOffset
        self.buffer.write_ulong(9)

        e = self.buffer.tell()

        self.buffer.seek(s)

        self.header = self.buffer.read(e - s)

        self._flush()

        self.headerWritten = True

    def _writeData(self, kind, time, data):
        """
        """
        if time < 0:
            raise ValueError('timestamp cannot be < 0')

        if time > 0xffffffff:
            raise OverflowError('timestamp too high %r' % (time, ))

        if not self.headerWritten:
            self._writeHeader()

            # PreviousTag0
            self.buffer.write_ulong(0)
            # empty audio tag -
            self.buffer.write('\x08\x00\x00\x00\x00\x00\x00\x00'
                              '\xEC\xF4\x1B\x00\x00\x00\x0B')

        if self.firstTimestamp is None:
            self.firstTimestamp = time

        time = time - self.firstTimestamp + self.totalTime

        if time < 0:
            time = 0

        self.previousTimestamp = time

        l = len(data)

        self.buffer.write_uchar(kind)
        self.buffer.write_24bit_uint(l)

        # timestamp
        self.buffer.write_24bit_uint(time & 0xffffff)
        self.buffer.write_uchar(time >> 24)

        # StreamID (always 0)
        self.buffer.write_24bit_uint(0)

        self.buffer.write(data)
        self.buffer.write_ulong(l + 11)

        self._flush()

    def start(self):
        """
        Prepare to receive FLV data.
        """
        if self.active:
            raise RuntimeError('Already active')

        self.active = True
        self.firstTimestamp = None
        self.previousTimestamp = 0

    def stop(self):
        """
        FLV data is complete.
        """
        if not self.active:
            raise RuntimeError('Already inactive')

        self.flush()

        self.active = False
        self.totalTime = self.previousTimestamp
        # empty audio tag -
        self.buffer.write('\x08\x00\x00\x00\x00\x00\x00\x00'
                          '\xEC\xF4\x1B\x00\x00\x00\x0B')

    def onMetaData(self, data):
        """
        Called when meta data has been received.

        @param dict: A C{dict} of name/value pairs.
        """
        if not self.active:
            raise RuntimeError('Meta data received without being active')

        if self.metaData:
            self.metaData.update(data)
        else:
            self.metaData = data

        b = pyamf.encode('onMetaData', self.metaData,
                         encoding=pyamf.AMF0).getvalue()
        self._writeData(FLVWriter.META_DATA, self.previousTimestamp, b)

    def audioDataReceived(self, data, time):
        """
        Called when audio data has been received.

        @param data: The encoded audio data.
        @type data: C{str}
        @param time: The timestamp associated with the data.
        @type data: C{int}
        """
        self._writeData(FLVWriter.AUDIO_DATA, time, data)

    def videoDataReceived(self, data, time):
        """
        Called when video data has been received.

        @param data: The encoded video data.
        @type data: C{str}
        @param time: The timestamp associated with the data.
        @type data: C{int}
        """
        self._writeData(FLVWriter.VIDEO_DATA, time, data)


class Client(server.Client):
    """
    Lets you handle each user, or client, connection to an application
    instance. The server automatically creates a Client object when a user
    connects to an application; the object is destroyed when the user
    disconnects from the application.

    @note: The functions in this class are just copies from live/main.asc -
        they don't do anything important (it seems).
    """

    def FCPublish(self, streamname):
        """
        Called by FME when publishing a stream. We make sure that an
        L{FLVWriter} is subscribed to the correct streamname.

        Note that the method name is case-sensitive.

        @param streamname: The name of the stream.
        @type streamname: C{str}
        """
        self.call('onFCPublish', {
            'code': 'NetStream.Publish.Start',
            'description': streamname})

    def FCUnpublish(self, streamname):
        """
        Called by FME when unpublishing a stream.

        Note that the method name is case-sensitive.

        @param streamname: The name of the stream.
        @type streamname: C{str}
        """
        self.call('onFCPublish', {
            'code': 'NetStream.Unpublish.Success',
            'description': streamname})

    def releaseStream(self, streamname):
        """
        Called by FME when it is about to publish a stream.

        @param streamname: The name of the stream.
        @type streamname: C{str}
        """

    def checkBandwidth(self):
        """
        """
        self.call('onBWDone')


class LiveStreamingApplication(server.Application):
    """
    """

    client = Client

    def startup(self):
        self.writers = {}

    def getWriter(self, name):
        """
        Returns a L{FLVWriter} corresponding to C{name}.

        @param name: The name of the stream.
        @type name: C{str}
        """
        return self.writers.get(name, None)

    def setWriter(self, name, writer):
        """
        """
        if name in self.writers:
            raise RuntimeError('Writer already exists'
                               ' for stream %s' % (name, ))

        self.writers[name] = writer

    def getStreamHandle(self, streamname):
        """
        """
        if os.path.sep in streamname:
            raise ValueError('Cannot have %s '
                             'in stream name' % (os.path.sep, ))

        name = '%s%sflv' % (os.path.join(os.getcwd(), streamname),
                            os.path.extsep)

        handle = open(name, 'wb+')

        return handle

    def onPublish(self, client, stream):
        """
        """
        writer = self.getWriter(stream.name)

        if writer is None:
            writer = FLVWriter(self.getStreamHandle(stream.name))
            self.setWriter(stream.name, writer)

        self.addSubscriber(stream, writer)
        writer.start()

    def onUnpublish(self, client, stream):
        """
        """
        writer = self.getWriter(stream.name)

        if writer:
            writer.stop()


if __name__ == '__main__':
    from twisted.internet import reactor

    app = LiveStreamingApplication()

    reactor.listenTCP(4340, server.ServerFactory({
        'live': app}))

    reactor.run()
