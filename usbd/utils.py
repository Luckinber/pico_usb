# MicroPython USB utility functions
# MIT license; Copyright (c) 2023 Angus Gratton
#
# Some constants and stateless utility functions for working with USB descriptors and requests.
from micropython import const
import machine
import struct

# Shared constants
#
# It's a tough decision of when to make a constant "shared" like this. "Private" constants have no resource use, but these will take up flash space for the name. Suggest deciding on basis of:
#
# - Is this constant used in a lot of places, including potentially by users
#   of this package?
#
# Otherwise, it's not the greatest sin to be copy-pasting "private" constants
# in a couple of places. I guess. :/

EP_IN_FLAG = const(1 << 7)

# Control transfer stages
STAGE_IDLE = const(0)
STAGE_SETUP = const(1)
STAGE_DATA = const(2)
STAGE_ACK = const(3)

# Request types
REQ_TYPE_STANDARD = const(0x0)
REQ_TYPE_CLASS = const(0x1)
REQ_TYPE_VENDOR = const(0x2)
REQ_TYPE_RESERVED = const(0x3)

# TinyUSB xfer_result_t enum
RESULT_SUCCESS = const(0)
RESULT_FAILED = const(1)
RESULT_STALLED = const(2)
RESULT_TIMEOUT = const(3)
RESULT_INVALID = const(4)


# Non-shared constants, used in this function only
_STD_DESC_INTERFACE_LEN = const(9)
_STD_DESC_INTERFACE_TYPE = const(0x4)
_STD_DESC_INTERFACE_ASSOC = const(0xB)

_STD_DESC_ENDPOINT_LEN = const(7)
_STD_DESC_ENDPOINT_TYPE = const(0x5)

_DESC_BUF_INITIAL_SIZE = const(128)

_INTERFACE_CLASS_VENDOR = const(0xFF)
_INTERFACE_SUBCLASS_NONE = const(0x00)

_PROTOCOL_NONE = const(0x00)

_ITF_ASSOCIATION_DESC_TYPE = const(0xB)  # Interface Association descriptor


class Descriptor:
    # Wrapper class for writing a descriptor in-place into a provided buffer
    #
    # Doesn't resize the buffer.
    #
    # Can be initialised with b=None to make a dummy run that calculates the
    # length needed for the buffer.
    def __init__(self, b):
        self.b = b
        self.o = 0  # offset of data written to the buffer

    def pack(self, fmt, *args):
        # Utility function to pack new data into the descriptor
        # buffer, starting at the current offset.
        #
        # Arguments are the same as struct.pack(), but it fills the
        # pre-allocated descriptor buffer (growing if needed), instead of
        # returning anything.
        self.pack_into(fmt, self.o, *args)

    def pack_into(self, fmt, offs, *args):
        # Utility function to pack new data into the descriptor at offset 'offs'.
        #
        # If the data written is before 'offs' then self.o isn't incremented,
        # otherwise it's incremented to point at the end of the written data.
        end = offs + struct.calcsize(fmt)
        if self.b:
            struct.pack_into(fmt, self.b, offs, *args)
        self.o = max(self.o, end)

    def append(self, a):
        # Append some bytes-like data to the descriptor
        if self.b:
            self.b[self.o : self.o + len(a)] = a
        self.o += len(a)

    # TODO: At the moment many of these arguments are named the same as the relevant field
    # in the spec, as this is easier to understand. Can save some code size by collapsing them
    # down.

    def interface(
        self,
        bInterfaceNumber,
        bNumEndpoints,
        bInterfaceClass=_INTERFACE_CLASS_VENDOR,
        bInterfaceSubClass=_INTERFACE_SUBCLASS_NONE,
        bInterfaceProtocol=_PROTOCOL_NONE,
        iInterface=0,
    ):
        # Utility function to append a standard Interface descriptor, with
        # the properties specified in the parameter list.
        #
        # Defaults for bInterfaceClass, SubClass and Protocol are a "vendor"
        # device.
        #
        # Note that iInterface is a string index number. If set, it should be set
        # by the caller USBInterface to the result of self._get_str_index(s),
        # where 's' is a string found in self.strs.
        self.pack(
            "BBBBBBBBB",
            _STD_DESC_INTERFACE_LEN,  # bLength
            _STD_DESC_INTERFACE_TYPE,  # bDescriptorType
            bInterfaceNumber,
            0,  # bAlternateSetting, not currently supported
            bNumEndpoints,
            bInterfaceClass,
            bInterfaceSubClass,
            bInterfaceProtocol,
            iInterface,
        )

    def endpoint(self, bEndpointAddress, bmAttributes, wMaxPacketSize, bInterval=1):
        # Utility function to append a standard Endpoint descriptor, with
        # the properties specified in the parameter list.
        #
        # See USB 2.0 specification section 9.6.6 Endpoint p269
        #
        # As well as a numeric value, bmAttributes can be a string value to represent
        # common endpoint types: "control", "bulk", "interrupt".
        if bmAttributes == "control":
            bmAttributes = 0
        elif bmAttributes == "bulk":
            bmAttributes = 2
        elif bmAttributes == "interrupt":
            bmAttributes = 3

        self.pack(
            "<BBBBHB",
            _STD_DESC_ENDPOINT_LEN,
            _STD_DESC_ENDPOINT_TYPE,
            bEndpointAddress,
            bmAttributes,
            wMaxPacketSize,
            bInterval,
        )

    def interface_assoc(
        self,
        bFirstInterface,
        bInterfaceCount,
        bFunctionClass,
        bFunctionSubClass,
        bFunctionProtocol=_PROTOCOL_NONE,
        iFunction=0,
    ):
        # Utility function to append an Interface Association descriptor,
        # with the properties specified in the parameter list.
        #
        # See USB ECN: Interface Association Descriptor.
        self.pack(
            "<BBBBBBBB",
            8,
            _ITF_ASSOCIATION_DESC_TYPE,
            bFirstInterface,
            bInterfaceCount,
            bFunctionClass,
            bFunctionSubClass,
            bFunctionProtocol,
            iFunction,
        )


def split_bmRequestType(bmRequestType):
    # Utility function to split control transfer field bmRequestType into a tuple of 3 fields:
    #
    # Recipient
    # Type
    # Data transfer direction
    #
    # See USB 2.0 specification section 9.3 USB Device Requests and 9.3.1 bmRequestType, p248.
    return (
        bmRequestType & 0x1F,
        (bmRequestType >> 5) & 0x03,
        (bmRequestType >> 7) & 0x01,
    )


class Buffer:
    # An interrupt-safe producer/consumer buffer that wraps a bytearray object.
    #
    # Kind of like a ring buffer, but supports the idea of returning a
    # memoryview for either read or write of multiple bytes (suitable for
    # passing to a buffer function without needing to allocate another buffer to
    # read into.)
    #
    # Consumer can call pend_read() to get a memoryview to read from, and then
    # finish_read(n) when done to indicate it read 'n' bytes from the
    # memoryview. There is also a readinto() convenience function.
    #
    # Producer must call pend_write() to get a memorybuffer to write into, and
    # then finish_write(n) when done to indicate it wrote 'n' bytes into the
    # memoryview. There is also a normal write() convenience function.
    #
    # - Only one producer and one consumer is supported.
    #
    # - Calling pend_read() and pend_write() is effectively idempotent, they can be
    #   called more than once without a corresponding finish_x() call if necessary
    #   (provided only one thread does this, as per the previous point.)
    #
    # - Calling finish_write() and finish_read() is hard interrupt safe (does
    #   not allocate). pend_read() and pend_write() each allocate 1 block for
    #   the memoryview that is returned.
    #
    # The buffer contents are always laid out as:
    #
    # - Slice [:_n] = bytes of valid data waiting to read
    # - Slice [_n:_w] = unused space
    # - Slice [_w:] = bytes of pending write buffer waiting to be written
    #
    # This buffer should be fast when most reads and writes are balanced and use
    # the whole buffer.  When this doesn't happen, performance degrades to
    # approximate a Python-based single byte ringbuffer.
    #
    def __init__(self, length):
        self._b = memoryview(bytearray(length))
        # number of bytes in buffer read to read, starting at index 0. Updated
        # by both producer & consumer.
        self._n = 0
        # start index of a pending write into the buffer, if any. equals
        # len(self._b) if no write is pending. Updated by producer only.
        self._w = length

    def writable(self):
        # Number of writable bytes in the buffer. Assumes no pending write is outstanding.
        return len(self._b) - self._n

    def readable(self):
        # Number of readable bytes in the buffer. Assumes no pending read is outstanding.
        return self._n

    def pend_write(self, wmax=None):
        # Returns a memoryview that the producer can write bytes into.
        # start the write at self._n, the end of data waiting to read
        #
        # If wmax is set then the memoryview is pre-sliced to be at most
        # this many bytes long.
        #
        # (No critical section needed as self._w is only updated by the producer.)
        self._w = self._n
        end = (self._w + wmax) if wmax else len(self._b)
        return self._b[self._w : end]

    def finish_write(self, nbytes):
        # Called by the producer to indicate it wrote nbytes into the buffer.
        ist = machine.disable_irq()
        try:
            assert nbytes <= len(self._b) - self._w  # can't say we wrote more than was pended
            if self._n == self._w:
                # no data was read while the write was happening, so the buffer is already in place
                # (this is the fast path)
                self._n += nbytes
            else:
                # Slow path: data was read while the write was happening, so
                # shuffle the newly written bytes back towards index 0 to avoid fragmentation
                #
                # As this updates self._n we have to do it in the critical
                # section, so do it byte by byte to avoid allocating.
                while nbytes > 0:
                    self._b[self._n] = self._b[self._w]
                    self._n += 1
                    self._w += 1
                    nbytes -= 1

            self._w = len(self._b)
        finally:
            machine.enable_irq(ist)

    def write(self, w):
        # Helper method for the producer to write into the buffer in one call
        pw = self.pend_write()
        to_w = min(len(w), len(pw))
        if to_w:
            pw[:to_w] = w[:to_w]
            self.finish_write(to_w)
        return to_w

    def pend_read(self):
        # Return a memoryview slice that the consumer can read bytes from
        return self._b[: self._n]

    def finish_read(self, nbytes):
        # Called by the consumer to indicate it read nbytes from the buffer.
        if not nbytes:
            return
        ist = machine.disable_irq()
        try:
            assert nbytes <= self._n  # can't say we read more than was available
            i = 0
            self._n -= nbytes
            while i < self._n:
                # consumer only read part of the buffer, so shuffle remaining
                # read data back towards index 0 to avoid fragmentation
                self._b[i] = self._b[i + nbytes]
                i += 1
        finally:
            machine.enable_irq(ist)

    def readinto(self, b):
        # Helper method for the consumer to read out of the buffer in one call
        pr = self.pend_read()
        to_r = min(len(pr), len(b))
        if to_r:
            b[:to_r] = pr[:to_r]
            self.finish_read(to_r)
        return to_r
