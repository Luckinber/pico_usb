# MicroPython USB device module
# MIT license; Copyright (c) 2022 Angus Gratton
from micropython import const, schedule
import machine
import struct
from io import BytesIO

from .utils import split_bmRequestType, EP_IN_FLAG, Descriptor

# USB descriptor types
_STD_DESC_DEV_TYPE = const(0x1)
_STD_DESC_CONFIG_TYPE = const(0x2)
_STD_DESC_STRING_TYPE = const(0x3)
_STD_DESC_INTERFACE_TYPE = const(0x4)
_STD_DESC_ENDPOINT_TYPE = const(0x5)
_STD_DESC_INTERFACE_ASSOC = const(0xB)

# Standard USB descriptor lengths
_STD_DESC_CONFIG_LEN = const(9)
_STD_DESC_INTERFACE_LEN = const(9)

_DESC_OFFSET_LEN = const(0)
_DESC_OFFSET_TYPE = const(1)

_DESC_OFFSET_INTERFACE_NUM = const(2)  # for _STD_DESC_INTERFACE_TYPE
_DESC_OFFSET_ENDPOINT_NUM = const(2)  # for _STD_DESC_ENDPOINT_TYPE

# Standard control request bmRequest fields, can extract by calling split_bmRequestType()
_REQ_RECIPIENT_DEVICE = const(0x0)
_REQ_RECIPIENT_INTERFACE = const(0x1)
_REQ_RECIPIENT_ENDPOINT = const(0x2)
_REQ_RECIPIENT_OTHER = const(0x3)

# Offsets into the standard configuration descriptor, to fixup
_OFFS_CONFIG_iConfiguration = const(6)

# Singleton _USBDevice instance
_dev = None

# These need to match the constants in tusb_config.h
_USB_STR_MANUF = const(0x01)
_USB_STR_PRODUCT = const(0x02)
_USB_STR_SERIAL = const(0x03)


def get():
    # Private function to access the singleton instance of the
    # MicroPython _USBDevice object
    #
    # (note this isn't the low-level object, the low-level object is
    # get()._usbd.)
    global _dev
    if not _dev:
        _dev = _USBDevice()
    return _dev


class _USBDevice:
    # Class that implements the Python parts of the MicroPython USBDevice.
    #
    # This class should only be instantiated by the _get() function above, never
    # directly.
    def __init__(self):
        self._itfs = {}  # Mapping from interface number to interface object, set by init()
        self._eps = {}  # Mapping from endpoint address to interface object, set by _open_cb()
        self._ep_cbs = {}  # Mapping from endpoint address to Optional[xfer callback]
        self._usbd = machine.USBDevice()  # low-level API

    def init(  # noqa: PLR0913 TODO: find a way to pass fewer arguments without wasting RAM
        self,
        *itfs,
        builtin_drivers=False,
        active=True,
        manufacturer_str=None,
        product_str=None,
        serial_str=None,
        configuration_str=None,
        id_vendor=None,
        id_product=None,
        bcd_device=None,
        device_class=None,
        device_subclass=None,
        device_protocol=None,
        config_str=None,
        max_power_ma=50,
    ):
        # Initialise the USB device with a set of interfaces, and optionally reconfiguring the
        # device and configuration descriptor fields

        _usbd = self._usbd

        _usbd.active(False)

        builtin = _usbd.builtin_driver = (
            _usbd.BUILTIN_DEFAULT if builtin_drivers else _usbd.BUILTIN_NONE
        )

        # Putting None for any strings that should fall back to the "built-in" value
        # Indexes in this list depends on _USB_STR_MANUF, _USB_STR_PRODUCT, _USB_STR_SERIAL
        strs = [None, manufacturer_str, product_str, serial_str]

        # Build the device descriptor
        FMT = "<BBHBBBBHHHBBBB"
        # read the static descriptor fields
        f = struct.unpack(FMT, builtin.desc_dev)

        def maybe_set(value, idx):
            # Override a numeric descriptor value or keep builtin value f[idx] if 'value' is None
            if value is not None:
                return value
            return f[idx]

        # Either copy each descriptor field directly from the builtin device descriptor, or 'maybe'
        # set it to the custom value from the object
        desc_dev = struct.pack(
            FMT,
            f[0],  # bLength
            f[1],  # bDescriptorType
            f[2],  # bcdUSB
            maybe_set(device_class, 3),  # bDeviceClass
            maybe_set(device_subclass, 4),  # bDeviceSubClass
            maybe_set(device_protocol, 5),  # bDeviceProtocol
            f[6],  # bMaxPacketSize0, TODO: allow overriding this value?
            maybe_set(id_vendor, 7),  # idVendor
            maybe_set(id_product, 8),  # idProduct
            maybe_set(bcd_device, 9),  # bcdDevice
            _USB_STR_MANUF,  # iManufacturer
            _USB_STR_PRODUCT,  # iProduct
            _USB_STR_SERIAL,  # iSerialNumber
            1,
        )  # bNumConfigurations

        # Iterate interfaces to build the configuration descriptor

        # Keep track of the interface and endpoint indexes
        itf_num = builtin.itf_max
        ep_num = max(builtin.ep_max, 1)  # Endpoint 0 always reserved for control
        while len(strs) < builtin.str_max:
            strs.append(None)  # Reserve other string indexes used by builtin drivers
        initial_cfg = builtin.desc_cfg or (b"\x00" * _STD_DESC_CONFIG_LEN)

        self._itfs = {}

        # Determine the total length of the configuration descriptor, by making dummy
        # calls to build the config descriptor
        desc = Descriptor(None)
        desc.append(initial_cfg)
        for itf in itfs:
            itf.desc_cfg(desc, 0, 0, [])

        # Allocate the real Descriptor helper to write into it, starting
        # after the standard configuration descriptor
        desc = Descriptor(bytearray(desc.o))
        desc.append(initial_cfg)
        for itf in itfs:
            itf.desc_cfg(desc, itf_num, ep_num, strs)

            for _ in range(itf.num_itfs()):
                self._itfs[itf_num] = itf  # Mapping from interface numbers to interfaces
                itf_num += 1

            ep_num += itf.num_eps()

        # Go back and update the Standard Configuration Descriptor
        # header at the start with values based on the complete
        # descriptor.
        #
        # See USB 2.0 specification section 9.6.3 p264 for details.
        bmAttributes = (
            (1 << 7)  # Reserved
            | (0 if max_power_ma else (1 << 6))  # Self-Powered
            # Remote Wakeup not currently supported
        )

        # Configuration string is optional but supported
        iConfiguration = 0
        if configuration_str:
            iConfiguration = len(strs)
            strs.append(configuration_str)

        desc.pack_into(
            "<BBHBBBBB",
            0,
            _STD_DESC_CONFIG_LEN,  # bLength
            _STD_DESC_CONFIG_TYPE,  # bDescriptorType
            len(desc.b),  # wTotalLength
            itf_num,
            1,  # bConfigurationValue
            iConfiguration,
            bmAttributes,
            max_power_ma,
        )

        _usbd.config(
            desc_dev,
            desc.b,
            strs,
            self._open_itf_cb,
            self._reset_cb,
            self._control_xfer_cb,
            self._xfer_cb,
        )
        _usbd.active(active)

    def _open_itf_cb(self, desc):
        # Singleton callback from TinyUSB custom class driver, when USB host does
        # Set Configuration. Called once per interface or IAD.

        # Note that even if the configuration descriptor contains an IAD, 'desc'
        # starts from the first interface descriptor in the IAD and not the IAD
        # descriptor.

        itf_num = desc[_DESC_OFFSET_INTERFACE_NUM]
        itf = self._itfs[itf_num]

        # Scan the full descriptor:
        # - Build _eps and _ep_addr from the endpoint descriptors
        # - Find the highest numbered interface provided to the callback
        #   (which will be the first interface, unless we're scanning
        #   multiple interfaces inside an IAD.)
        self._eps = {}
        self._ep_cbs = {}
        offs = 0
        max_itf = itf_num
        while offs < len(desc):
            dl = desc[offs + _DESC_OFFSET_LEN]
            dt = desc[offs + _DESC_OFFSET_TYPE]
            if dt == _STD_DESC_ENDPOINT_TYPE:
                ep_addr = desc[offs + _DESC_OFFSET_ENDPOINT_NUM]
                self._eps[ep_addr] = itf
                self._ep_cbs[ep_addr] = None
            elif dt == _STD_DESC_INTERFACE_TYPE:
                max_itf = max(max_itf, desc[offs + _DESC_OFFSET_INTERFACE_NUM])
            offs += dl

        # If 'desc' is not the inside of an Interface Association Descriptor but
        # 'itf' object still represents multiple USB interfaces (i.e. MIDI),
        # defer calling 'itf.handle_open()' until this callback fires for the
        # highest numbered USB interface.
        #
        # This means handle_open() is only called once, and that it can
        # safely submit transfers on any of the USB interfaces' endpoints.
        if self._itfs.get(max_itf + 1, None) != itf:
            itf.handle_open()

    def _reset_cb(self):
        # Callback when the USB device is reset by the host

        # Cancel outstanding transfer callbacks
        for k in self._ep_cbs.keys():
            self._ep_cbs[k] = None

        # Allow interfaces to respond to the reset
        for itf in self._itfs.values():
            itf.handle_reset()

    def _submit_xfer(self, ep_addr, data, done_cb=None):
        # Singleton function to submit a USB transfer (of any type except control).
        #
        # Generally, drivers should call USBInterface.submit_xfer() instead. See
        # that function for documentation about the possible parameter values.
        if ep_addr not in self._eps:
            raise ValueError("ep_addr")
        if self._ep_cbs[ep_addr]:
            raise RuntimeError("xfer_pending")

        # USBDevice callback may be called immediately, before Python execution
        # continues, so set it first.
        #
        # To allow xfer_pending checks to work, store True instead of None.
        self._ep_cbs[ep_addr] = done_cb or True
        return self._usbd.submit_xfer(ep_addr, data)

    def _xfer_cb(self, ep_addr, result, xferred_bytes):
        # Singleton callback from TinyUSB custom class driver when a transfer completes.
        cb = self._ep_cbs.get(ep_addr, None)
        self._ep_cbs[ep_addr] = None
        if callable(cb):
            cb(ep_addr, result, xferred_bytes)

    def _control_xfer_cb(self, stage, request):
        # Singleton callback from TinyUSB custom class driver when a control
        # transfer is in progress.
        #
        # stage determines appropriate responses (possible values
        # utils.STAGE_SETUP, utils.STAGE_DATA, utils.STAGE_ACK).
        #
        # The TinyUSB class driver framework only calls this function for
        # particular types of control transfer, other standard control transfers
        # are handled by TinyUSB itself.
        wIndex = request[4] + (request[5] << 8)
        recipient, _, _ = split_bmRequestType(request[0])

        itf = None
        result = None

        if recipient == _REQ_RECIPIENT_DEVICE:
            itf = self._itfs.get(wIndex & 0xFFFF, None)
            if itf:
                result = itf.handle_device_control_xfer(stage, request)
        elif recipient == _REQ_RECIPIENT_INTERFACE:
            itf = self._itfs.get(wIndex & 0xFFFF, None)
            if itf:
                result = itf.handle_interface_control_xfer(stage, request)
        elif recipient == _REQ_RECIPIENT_ENDPOINT:
            ep_num = wIndex & 0xFFFF
            itf = self._eps.get(ep_num, None)
            if itf:
                result = itf.handle_endpoint_control_xfer(stage, request)

        if not itf:
            # At time this code was written, only the control transfers shown
            # above are passed to the class driver callback. See
            # invoke_class_control() in tinyusb usbd.c
            raise RuntimeError(f"Unexpected control request type {request[0]:#x}")

        # Expecting any of the following possible replies from
        # handle_NNN_control_xfer():
        #
        # True - Continue transfer, no data
        # False - STALL transfer
        # Object with buffer interface - submit this data for the control transfer
        return result


class USBInterface:
    # Abstract base class to implement USB Interface (and associated endpoints),
    # or a collection of USB Interfaces, in Python
    #
    # (Despite the name an object of type USBInterface can represent multiple
    # associated interfaces, with or without an Interface Association Descriptor
    # prepended to them. Override num_itfs() if assigning >1 USB interface.)

    def __init__(self):
        self._open = False

    def desc_cfg(self, desc, itf_num, ep_num, strs):
        # Function to build configuration descriptor contents for this interface
        # or group of interfaces. This is called on each interface from
        # USBDevice.init().
        #
        # This function should insert:
        #
        # - At least one standard Interface descriptor (can call
        # - desc.interface()).
        #
        # Plus, optionally:
        #
        # - One or more endpoint descriptors (can call desc.endpoint()).
        # - An Interface Association Descriptor, prepended before.
        # - Other class-specific configuration descriptor data.
        #
        # This function is called twice per call to USBDevice.init(). The first
        # time the values of all arguments are dummies that are used only to
        # calculate the total length of the descriptor. Therefore, anything this
        # function does should be idempotent and it should add the same
        # descriptors each time. If saving interface numbers or endpoint numbers
        # for later
        #
        # Parameters:
        #
        # - desc - Descriptor helper to write the configuration descriptor bytes into.
        #   The first time this function is called 'desc' is a dummy object
        #   with no backing buffer (exists to count the number of bytes needed).
        #
        # - itf_num - First bNumInterfaces value to assign. The descriptor
        #   should contain the same number of interfaces returned by num_itfs(),
        #   starting from this value.
        #
        # - ep_num - Address of the first available endpoint number to use for
        #   endpoint descriptor addresses. Subclasses should save the
        #   endpoint addresses selected, to look up later (although note the first
        #   time this function is called, the values will be dummies.)
        #
        # - strs - list of string descriptors for this USB device. This function
        #   can append to this list, and then insert the index of the new string
        #   in the list into the configuration descriptor.
        raise NotImplementedError

    def num_itfs(self):
        # Return the number of actual USB Interfaces represented by this object
        # (as set in desc_cfg().)
        #
        # Only needs to be overriden if implementing a USBInterface class that
        # represents more than one USB Interface descriptor (i.e. MIDI), or an
        # Interface Association Descriptor (i.e. USB-CDC).
        return 1

    def num_eps(self):
        # Return the number of USB Endpoint numbers represented by this object
        # (as set in desc_cfg().)
        #
        # Note for each count returned by this function, the interface may
        # choose to have both an IN and OUT endpoint (i.e. IN flag is not
        # considered a value here.)
        #
        # This value can be zero, if the USB Host only communicates with this
        # interface using control transfers.
        return 0

    def handle_open(self):
        # Callback called when the USB host accepts the device configuration.
        #
        # Override this function to initiate any operations that the USB interface
        # should do when the USB device is configured to the host.
        self._open = True

    def handle_reset(self):
        # Callback called on every registered interface when the USB device is
        # reset by the host. This can happen when the USB device is unplugged,
        # or if the host triggers a reset for some other reason.
        #
        # Override this function to cancel any pending operations specific to
        # the interface (outstanding USB transfers are already cancelled).
        #
        # At this point, no USB functionality is available - handle_open() will
        # be called later if/when the USB host re-enumerates and configures the
        # interface.
        self._open = False

    def is_open(self):
        # Returns True if the interface is in use
        return self._open

    def handle_device_control_xfer(self, stage, request):
        # Control transfer callback. Override to handle a non-standard device
        # control transfer where bmRequestType Recipient is Device, Type is
        # utils.REQ_TYPE_CLASS, and the lower byte of wIndex indicates this interface.
        #
        # (See USB 2.0 specification 9.4 Standard Device Requests, p250).
        #
        # This particular request type seems pretty uncommon for a device class
        # driver to need to handle, most hosts will not send this so most
        # implementations won't need to override it.
        #
        # Parameters:
        #
        # - stage is one of utils.STAGE_SETUP, utils.STAGE_DATA, utils.STAGE_ACK.
        #
        # - request is a memoryview into a USB request packet, as per USB 2.0
        #   specification 9.3 USB Device Requests, p250.  the memoryview is only
        #   valid while the callback is running.
        #
        # The function can call split_bmRequestType(request[0]) to split
        # bmRequestType into (Recipient, Type, Direction).
        #
        # Result, any of:
        #
        # - True to continue the request, False to STALL the endpoint.
        # - Buffer interface object to provide a buffer to the host as part of the
        #   transfer, if applicable.
        return False

    def handle_interface_control_xfer(self, stage, request):
        # Control transfer callback. Override to handle a device control
        # transfer where bmRequestType Recipient is Interface, and the lower byte
        # of wIndex indicates this interface.
        #
        # (See USB 2.0 specification 9.4 Standard Device Requests, p250).
        #
        # bmRequestType Type field may have different values. It's not necessary
        # to handle the mandatory Standard requests (bmRequestType Type ==
        # utils.REQ_TYPE_STANDARD), if the driver returns False in these cases then
        # TinyUSB will provide the necessary responses.
        #
        # See handle_device_control_xfer() for a description of the arguments and
        # possible return values.
        return False

    def handle_endpoint_control_xfer(self, stage, request):
        # Control transfer callback. Override to handle a device
        # control transfer where bmRequestType Recipient is Endpoint and
        # the lower byte of wIndex indicates an endpoint address associated
        # with this interface.
        #
        # bmRequestType Type will generally have any value except
        # utils.REQ_TYPE_STANDARD, as Standard endpoint requests are handled by
        # TinyUSB. The exception is the the Standard "Set Feature" request. This
        # is handled by Tiny USB but also passed through to the driver in case it
        # needs to change any internal state, but most drivers can ignore and
        # return False in this case.
        #
        # (See USB 2.0 specification 9.4 Standard Device Requests, p250).
        #
        # See handle_device_control_xfer() for a description of the parameters and
        # possible return values.
        return False

    def xfer_pending(self, ep_addr):
        # Return True if a transfer is already pending on ep_addr.
        #
        # Only one transfer can be submitted at a time.
        return _dev and bool(_dev._ep_cbs[ep_addr])

    def submit_xfer(self, ep_addr, data, done_cb=None):
        # Submit a USB transfer (of any type except control)
        #
        # Parameters:
        #
        # - ep_addr. Address of the endpoint to submit the transfer on. Caller is
        #   responsible for ensuring that ep_addr is correct and belongs to this
        #   interface. Only one transfer can be active at a time on each endpoint.
        #
        # - data. Buffer containing data to send, or for data to be read into
        #   (depending on endpoint direction).
        #
        # - done_cb. Optional callback function for when the transfer
        # completes. The callback is called with arguments (ep_addr, result,
        # xferred_bytes) where result is one of xfer_result_t enum (see top of
        # this file), and xferred_bytes is an integer.
        #
        # If the function returns, the transfer is queued.
        #
        # The function will raise RuntimeError under the following conditions:
        #
        # - The interface is not "open" (i.e. has not been enumerated and configured
        #   by the host yet.)
        #
        # - A transfer is already pending on this endpoint (use xfer_pending() to check
        #   before sending if needed.)
        #
        # - A DCD error occurred when queueing the transfer on the hardware.
        #
        #
        # Will raise TypeError if 'data' isn't he correct type of buffer for the
        # endpoint transfer direction.
        #
        # Note that done_cb may be called immediately, possibly before this
        # function has returned to the caller.
        if not self._open:
            raise RuntimeError("Not open")
        _dev._submit_xfer(ep_addr, data, done_cb)

    def stall(self, ep_addr, *args):
        # Set or get the endpoint STALL state.
        #
        # To get endpoint stall stage, call with a single argument.
        # To set endpoint stall state, call with an additional boolean
        # argument to set or clear.
        #
        # Generally endpoint STALL is handled automatically, but there are some
        # device classes that need to explicitly stall or unstall an endpoint
        # under certain conditions.
        if not self._open or ep_addr not in self._eps:
            raise RuntimeError
        _dev._usbd.stall(ep_addr, *args)