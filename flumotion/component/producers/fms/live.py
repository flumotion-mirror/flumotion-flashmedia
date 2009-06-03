
"""
A port of the main.asc from the live application included in FMS's sample
applications.
"""

import sys, os.path

sys.path.insert(0, os.getcwd())

import re
import pyamf.util
from twisted.internet import defer, reactor

from rtmpy import server, rtmp


# Uncomment this to see lots of debugging output
# rtmp.DEBUG = True


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
        if time < self.lastTimestamp:
            raise RuntimeError('Negative timestamp detected %r' % (time,))

        t = time + self.totalTime

        if t < 0:
            raise ValueError('timestamp cannot be < 0')

        if t > 0xffffffff:
            raise OverflowError('timestamp too high %r' % (t,))

        if not self.headerWritten:
            self._writeHeader()

            # PreviousTag0
            self.buffer.write_ulong(0)

        l = len(data)
        self.lastTimestamp = time

        self.buffer.write_uchar(kind)
        self.buffer.write_24bit_uint(l)

        # timestamp
        self.buffer.write_24bit_uint(t & 0xffffff)
        self.buffer.write_uchar(t >> 24)

        # StreamID (always 0)
        self.buffer.write_24bit_uint(0)

        self.buffer.write(data)
        self.buffer.write_ulong(l + 11)

        self._flush()

    def streamPublished(self):
        """
        Prepare to receive FLV data.
        """
        if self.active:
            raise RuntimeError('Already active')

        self.active = True
        self.lastTimestamp = 0

    def streamUnpublished(self):
        """
        FLV data is complete.
        """
        if not self.active:
            raise RuntimeError('Already inactive')

        self.flush()

        self.active = False
        self.totalTime += self.lastTimestamp

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

        b = pyamf.encode('onMetaData', self.metaData).getvalue()
        self._writeData(FLVWriter.META_DATA, self.lastTimestamp, b)

    def audioDataReceived(self, data, time):
        """
        Called when audio data has been received.

        @param data: The encoded audio data.
        @type data: C{str}
        @param time: The timestamp associated with the data.
        @type data: C{int}
        """
        if not self.active:
            raise RuntimeError('Audio data received without being active')

        self._writeData(FLVWriter.AUDIO_DATA, time, data)

    def videoDataReceived(self, data, time):
        """
        Called when video data has been received.

        @param data: The encoded video data.
        @type data: C{str}
        @param time: The timestamp associated with the data.
        @type data: C{int}
        """
        if not self.active:
            raise RuntimeError('Video data received without being active')

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

    def __init__(self, *args, **kwargs):
        server.Client.__init__(self, *args, **kwargs)

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
            'description': streamname
        })

    def FCUnpublish(self, streamname):
        """
        Called by FME when unpublishing a stream.

        Note that the method name is case-sensitive.

        @param streamname: The name of the stream.
        @type streamname: C{str}
        """
        self.call('onFCPublish', {
            'code': 'NetStream.Unpublish.Success',
            'description': streamname
        })

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

    def onConnect(self, client, **kwargs):
        """
        Called when connection request has been made by the client. C{False}
        must be returned if the application rejects the client. A
        L{twisted.internet.defer.Deferred} can also be returned (the same rule
        still applies).

        @param client: The client object attempting to connect to this
            application.
        @type client: An instance of L{LiveStreamingApplication.client} (in
            this case L{Client})
        @param kwargs: A dict of key/value pairs that were sent as part of the
            connection request.
        @return: A L{twisted.internet.defer.Deferred} or a C{bool}. Returning
            a C{False} value will reject the connection.
        """
        # TODO: integrate twisted.cred
        username, password = self.factory.parseCredentials(kwargs['app'])

        if not (username == 'foo' and password == 'bar'):
            return False

        return True

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
            raise RuntimeError('Writer already exists for stream %s' % (name,))

        self.writers[name] = writer

    def getStreamHandle(self, streamname):
        """
        """
        if os.path.sep in streamname:
            raise ValueError('Cannot have %s in stream name' % (os.path.sep,))

        name = '%s%sflv' % (os.path.join(os.getcwd(), streamname), os.path.extsep)

        handle = open(name, 'wb+')

        return handle

    def onPublish(self, client, stream):
        """
        Called when a client is requesting to publish a stream. Return
        C{False} to reject the publish request (or a L{defer.Deferred} which
        returns the bool).
        """
        if os.path.sep in stream.name:
            return False

        writer = self.getWriter(stream.name)

        if writer is None:
            handle = self.getStreamHandle(stream.name)
            writer = FLVWriter(handle)

            stream.addSubscriber(writer)

            self.setWriter(stream.name, writer)


class Server(server.ServerFactory):
    """
    This class is only necessary to support custom application names. FME does
    not support argument passing in its connection url so we have to do a
    workaround.

    An example url:
      - rtmp://192.168.251.10/live

    To specify a username/password use a specially constructed url like:
      - rtmp://192.168.251.10/live/username=foo/password=bar

    It is important to note that the username or password cannot contain '/'
    characters, otherwise the arg parsing will not work.
    """

    url_re = re.compile('([^\/]*)(\/(username=([^\/]*))?(\/(password=([^\/]*))?)?)?\/?')

    def parseCredentials(self, s):
        """
        Parses the username/password from an application connection param.

        @param s: The connection string. E.g. 'live/username=foo/password=bar'
        @type s: C{str}
        @return: A tuple containing the username and password (in that order).
            If a match for either is not found, C{None} will be substituted in
            its place.
        """
        m = self.url_re.match(s)

        if m is None:
            return None, None

        g = m.groups()
        username, password = None, None

        try:
            username = g[3]
        except IndexError:
            pass

        try:
            password = g[6]
        except IndexError:
            pass

        return username, password

    def getApplication(self, name):
        """
        Returns the application object based on C{name}. Strips any credential
        info first.

        @param name: The application name.
        @type name: C{str}
        @return: The application object associated with the name. If no such
            association exists then C{None} should be returned.
        @rtype: L{server.Application} instance or C{None}.
        """
        m = self.url_re.match(name)

        if m is None:
            return server.ServerFactory.getApplication(self, name)

        return server.ServerFactory.getApplication(self, m.groups()[0])

