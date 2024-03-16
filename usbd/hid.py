# MicroPython USB hid module
# MIT license; Copyright (c) 2023 Angus Gratton
from micropython import const
import struct
import time
from .device import USBInterface
from .utils import (
    Descriptor,
    split_bmRequestType,
    EP_IN_FLAG,
    STAGE_SETUP,
    STAGE_DATA,
    REQ_TYPE_STANDARD,
    REQ_TYPE_CLASS,
)

_DESC_HID_TYPE = const(0x21)
_DESC_REPORT_TYPE = const(0x22)
_DESC_PHYSICAL_TYPE = const(0x23)

_INTERFACE_CLASS = const(0x03)
_INTERFACE_SUBCLASS_NONE = const(0x00)
_INTERFACE_SUBCLASS_BOOT = const(0x01)

_INTERFACE_PROTOCOL_NONE = const(0x00)
_INTERFACE_PROTOCOL_KEYBOARD = const(0x01)
_INTERFACE_PROTOCOL_MOUSE = const(0x02)

# bRequest values for HID control requests
_REQ_CONTROL_GET_REPORT = const(0x01)
_REQ_CONTROL_GET_IDLE = const(0x02)
_REQ_CONTROL_GET_PROTOCOL = const(0x03)
_REQ_CONTROL_GET_DESCRIPTOR = const(0x06)
_REQ_CONTROL_SET_REPORT = const(0x09)
_REQ_CONTROL_SET_IDLE = const(0x0A)
_REQ_CONTROL_SET_PROTOCOL = const(0x0B)

_STD_DESC_INTERFACE_LEN = const(9)
_STD_DESC_ENDPOINT_LEN = const(7)


class HIDInterface(USBInterface):
    # Abstract base class to implement a USB device HID interface in Python.

    def __init__(
        self,
        report_descriptor,
        extra_descriptors=[],
        set_report_buf=None,
        protocol=_INTERFACE_PROTOCOL_NONE,
        interface_str=None,
    ):
        # Construct a new HID interface.
        #
        # - report_descriptor is the only mandatory argument, which is the binary
        # data consisting of the HID Report Descriptor. See Device Class
        # Definition for Human Interface Devices (HID) v1.11 section 6.2.2 Report
        # Descriptor, p23.
        #
        # - extra_descriptors is an optional argument holding additional HID
        #   descriptors, to append after the mandatory report descriptor. Most
        #   HID devices do not use these.
        #
        # - set_report_buf is an optional writable buffer object (i.e.
        #   bytearray), where SET_REPORT requests from the host can be
        #   written. Only necessary if the report_descriptor contains Output
        #   entries. If set, the size must be at least the size of the largest
        #   Output entry.
        #
        # - protocol can be set to a specific value as per HID v1.11 section 4.3 Protocols, p9.
        #
        # - interface_str is an optional string descriptor to associate with the HID USB interface.
        super().__init__()
        self.report_descriptor = report_descriptor
        self.extra_descriptors = extra_descriptors
        self._set_report_buf = set_report_buf
        self.protocol = protocol
        self.interface_str = interface_str

        self._int_ep = None  # set during enumeration

    def get_report(self):
        return False

    def handle_set_report(self, report_data, report_id, report_type):
        # Override this function in order to handle SET REPORT requests from the host,
        # where it sends data to the HID device.
        #
        # This function will only be called if the Report descriptor contains at least one Output entry,
        # and the set_report_buf argument is provided to the constructor.
        #
        # Return True to complete the control transfer normally, False to abort it.
        return True

    def send_report(self, report_data, timeout_ms=100):
        # Helper function to send a HID report in the typical USB interrupt
        # endpoint associated with a HID interface.
        #
        # Returns True if successful, False if HID device is not active or timeout
        # is reached without being able to send the report.
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while self.is_open() and self.xfer_pending(self._int_ep):
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                return False
            time.sleep_ms(50)
        if not self.is_open():
            return False
        self.submit_xfer(self._int_ep, report_data)

    def desc_cfg(self, desc, itf_num, ep_num, strs):
        # Add the standard interface descriptor
        desc.interface(
            itf_num,
            1,
            _INTERFACE_CLASS,
            _INTERFACE_SUBCLASS_NONE,
            self.protocol,
            len(strs) if self.interface_str else 0,
        )

        if self.interface_str:
            strs.append(self.interface_str)

        # As per HID v1.11 section 7.1 Standard Requests, return the contents of
        # the standard HID descriptor before the associated endpoint descriptor.
        self.get_hid_descriptor(desc)

        # Add the typical single USB interrupt endpoint descriptor associated
        # with a HID interface.
        self._int_ep = ep_num | EP_IN_FLAG
        desc.endpoint(self._int_ep, "interrupt", 8, 8)

        self.idle_rate = 0
        self.protocol = 0

    def num_eps(self):
        return 1

    def get_hid_descriptor(self, desc=None):
        # Append a full USB HID descriptor from the object's report descriptor
        # and optional additional descriptors.
        #
        # See HID Specification Version 1.1, Section 6.2.1 HID Descriptor p22

        l = 9 + 3 * len(self.extra_descriptors)  # total length

        if desc is None:
            desc = Descriptor(bytearray(l))

        desc.pack(
            "<BBHBBBH",
            l,  # bLength
            _DESC_HID_TYPE,  # bDescriptorType
            0x111,  # bcdHID
            0,  # bCountryCode
            len(self.extra_descriptors) + 1,  # bNumDescriptors
            0x22,  # bDescriptorType, Report
            len(self.report_descriptor),  # wDescriptorLength, Report
        )
        # Fill in any additional descriptor type/length pairs
        #
        # TODO: unclear if this functionality is ever used, may be easier to not
        # support in base class
        for dt, dd in self.extra_descriptors:
            desc.pack("<BH", dt, len(dd))

        return desc.b

    def handle_interface_control_xfer(self, stage, request):
        # Handle standard and class-specific interface control transfers for HID devices.
        bmRequestType, bRequest, wValue, _, wLength = struct.unpack("BBHHH", request)

        recipient, req_type, _ = split_bmRequestType(bmRequestType)

        if stage == STAGE_SETUP:
            if req_type == REQ_TYPE_STANDARD:
                # HID Spec p48: 7.1 Standard Requests
                if bRequest == _REQ_CONTROL_GET_DESCRIPTOR:
                    desc_type = wValue >> 8
                    if desc_type == _DESC_HID_TYPE:
                        return self.get_hid_descriptor()
                    if desc_type == _DESC_REPORT_TYPE:
                        return self.report_descriptor
            elif req_type == REQ_TYPE_CLASS:
                # HID Spec p50: 7.2 Class-Specific Requests
                if bRequest == _REQ_CONTROL_GET_REPORT:
                    print("GET_REPORT?")
                    return False  # Unsupported for now
                if bRequest == _REQ_CONTROL_GET_IDLE:
                    return bytes([self.idle_rate])
                if bRequest == _REQ_CONTROL_GET_PROTOCOL:
                    return bytes([self.protocol])
                if bRequest == _REQ_CONTROL_SET_IDLE:
                    self.idle_rate = wValue >> 8
                    return b""
                if bRequest == _REQ_CONTROL_SET_PROTOCOL:
                    self.protocol = wValue
                    return b""
                if bRequest == _REQ_CONTROL_SET_REPORT:
                    # Return the _set_report_buf to be filled with the
                    # report data
                    if not self._set_report_buf:
                        return False
                    elif wLength >= len(self._set_report_buf):
                        # Saves an allocation if the size is exactly right (or will be a short read)
                        return self._set_report_buf
                    else:
                        # Otherwise, need to wrap the buffer in a memoryview of the correct length
                        #
                        # TODO: check this is correct, maybe TinyUSB won't mind if we ask for more
                        # bytes than the host has offered us.
                        return memoryview(self._set_report_buf)[:wLength]
            return False  # Unsupported

        if stage == STAGE_DATA:
            if req_type == REQ_TYPE_CLASS:
                if bRequest == _REQ_CONTROL_SET_REPORT and self._set_report_buf:
                    report_id = wValue & 0xFF
                    report_type = wValue >> 8
                    report_data = self._set_report_buf
                    if wLength < len(report_data):
                        # as above, need to truncate the buffer if we read less
                        # bytes than what was provided
                        report_data = memoryview(self._set_report_buf)[:wLength]
                    self.handle_set_report(report_data, report_id, report_type)

        return True  # allow DATA/ACK stages to complete normally
