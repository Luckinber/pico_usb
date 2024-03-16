#!/usr/bin/env python
#
# Internal performance and reliability test for USB CDC.
#
# MIT License; Original Copyright (c) Damien George, updated by Angus Gratton 2023.
#
# Runs on the host, not the device.
#
# Usage:
#   cdc_rate_benchmark.py [REPL serial device] [DATA serial device]
#
# - If both REPL and DATA serial devices are specified, script is loaded onto the REPL device
#   and data is measured over the DATA device.
# - If only REPL serial device argument is specified, same port is used for both "REPL" and "DATA"
# - If neither serial device is specified, defaults to /dev/ttyACM0.
#

import sys
import time
import argparse
import serial


def drain_input(ser):
    time.sleep(0.1)
    while ser.inWaiting() > 0:
        data = ser.read(ser.inWaiting())
        time.sleep(0.1)


test_script_common = """
try:
    import pyb
    p = pyb.USB_VCP(vcp_id)
    pyb.LED(1).on()
    led = pyb.LED(2)
except ImportError:
    try:
        from usbd import CDC, get_usbdevice
        cdc = CDC(timeout=60_000)  # adds itself automatically
        ud = get_usbdevice()
        ud.reenumerate()
        p = cdc
        led = None
    except ImportError:
        import sys
        p = sys.stdout.buffer
        led = None
"""

read_test_script = """
vcp_id = %u
b=bytearray(%u)
assert p.read(1) == b'G'  # Trigger
for i in range(len(b)):
    b[i] = i & 0xff
for _ in range(%d):
    if led:
        led.toggle()
    n = p.write(b)
    assert n == len(b)
p.flush()  # for dynamic CDC, need to send all bytes before 'p' may be garbage collected
"""


def read_test(ser_repl, ser_data, usb_vcp_id, bufsize, nbuf):
    assert bufsize % 256 == 0  # for verify to work

    # Load and run the read_test_script.
    ser_repl.write(b"\x03\x03\x01\x04")  # break, break, raw-repl, soft-reboot
    drain_input(ser_repl)
    ser_repl.write(bytes(test_script_common, "ascii"))
    ser_repl.write(bytes(read_test_script % (usb_vcp_id, bufsize, nbuf), "ascii"))
    ser_repl.write(b"\x04")  # eof
    ser_repl.flush()
    response = ser_repl.read(2)
    assert response == b"OK", response

    # for dynamic USB CDC this port doesn't exist until shortly after the script runs, and we need
    # to reopen it for each test run
    dynamic_cdc = False
    if isinstance(ser_data, str):
        time.sleep(2)  # hacky, but need some time for old port to close and new one to appear
        ser_data = serial.Serial(ser_data, baudrate=115200)
        dynamic_cdc = True

    ser_data.write(b"G")  # trigger script to start sending

    # Read data from the device, check it is correct, and measure throughput.
    n = 0
    last_byte = None
    t_start = time.time()
    remain = nbuf * bufsize
    READ_TIMEOUT = 1e9
    total_data = bytearray(remain)
    while remain:
        t0 = time.monotonic_ns()
        while ser_data.inWaiting() == 0:
            if time.monotonic_ns() - t0 > READ_TIMEOUT:
                # timeout waiting for data from device
                break
            time.sleep(0.0001)
        if not ser_data.inWaiting():
            print(f"ERROR: timeout waiting for data. remain={remain}")
            break
        to_read = min(ser_data.inWaiting(), remain)
        data = ser_data.read(to_read)
        # verify bytes coming in are in sequence
        # if last_byte is not None:
        #    if data[0] != (last_byte + 1) & 0xff:
        #        print('ERROR: first byte is not in sequence:', last_byte, data[0])
        # last_byte = data[-1]
        # for i in range(1, len(data)):
        #    if data[i] != (data[i - 1] + 1) & 0xff:
        #        print('ERROR: data not in sequence at position %d:' % i, data[i - 1], data[i])
        remain -= len(data)
        # print(n, nbuf * bufsize, end="\r")
        total_data[n : n + len(data)] = data
        n += len(data)
    t_end = time.time()
    for i in range(len(total_data)):
        if total_data[i] != i & 0xFF:
            print("fail", i, i & 0xFF, total_data[i])
    ser_repl.write(b"\x03")  # break
    t = t_end - t_start

    # Print results.
    print(
        "READ: bufsize=%u, read %u bytes in %.2f msec = %.2f kibytes/sec = %.2f MBits/sec"
        % (bufsize, n, t * 1000, n / 1024 / t, n * 8 / 1000000 / t)
    )

    if dynamic_cdc:
        ser_data.close()

    return t


write_test_script = """
import sys
vcp_id = %u
b=bytearray(%u)
while 1:
    if led:
        led.toggle()
    n = p.readinto(b)
    assert n is not None  # timeout
    fail = 0
    er = b'ER'
    if %u:
        for i in range(n):
            if b[i] != 32 + (i & 0x3f):
                fail += 1
    if n != len(b):
        er = b'BL'
        fail = n or -1

    if fail:
        sys.stdout.write(er + b'%%04u' %% fail)
    else:
        sys.stdout.write(b'OK%%04u' %% n)
"""


def write_test(ser_repl, ser_data, usb_vcp_id, bufsize, nbuf, verified):
    # Load and run the write_test_script.
    # ser_repl.write(b'\x03\x03\x01\x04') # break, break, raw-repl, soft-reboot
    ser_repl.write(b"\x03\x01\x04")  # break, raw-repl, soft-reboot
    drain_input(ser_repl)
    ser_repl.write(bytes(test_script_common, "ascii"))
    ser_repl.write(bytes(write_test_script % (usb_vcp_id, bufsize, 1 if verified else 0), "ascii"))
    ser_repl.write(b"\x04")  # eof
    ser_repl.flush()
    drain_input(ser_repl)

    # for dynamic USB CDC this port doesn't exist until shortly after the script runs, and we need
    # to reopen it for each test run
    dynamic_cdc = False
    if isinstance(ser_data, str):
        time.sleep(2)  # hacky, but need some time for old port to close and new one to appear
        ser_data = serial.Serial(ser_data, baudrate=115200)
        dynamic_cdc = True

    # Write data to the device, check it is correct, and measure throughput.
    n = 0
    t_start = time.time()
    buf = bytearray(bufsize)
    for i in range(len(buf)):
        buf[i] = 32 + (i & 0x3F)  # don't want to send ctrl chars!
    for i in range(nbuf):
        ser_data.write(buf)
        n += len(buf)
        # while ser_data.inWaiting() == 0:
        #    time.sleep(0.001)
        # response = ser_data.read(ser_data.inWaiting())
        response = ser_repl.read(6)
        if response != b"OK%04u" % bufsize:
            response += ser_repl.read(ser_repl.inWaiting())
            print("bad response, expecting OK%04u, got %r" % (bufsize, response))
    t_end = time.time()
    ser_repl.write(b"\x03")  # break
    t = t_end - t_start

    # Print results.
    print(
        "WRITE: verified=%d, bufsize=%u, wrote %u bytes in %.2f msec = %.2f kibytes/sec = %.2f MBits/sec"
        % (verified, bufsize, n, t * 1000, n / 1024 / t, n * 8 / 1000000 / t)
    )

    if dynamic_cdc:
        ser_data.close()

    return t


def main():
    dev_repl = "/dev/ttyACM0"
    dev_data = None
    if len(sys.argv) >= 2:
        dev_repl = sys.argv[1]
    if len(sys.argv) >= 3:
        assert len(sys.argv) >= 4
        dev_data = sys.argv[2]
        usb_vcp_id = int(sys.argv[3])

    if dev_data is None:
        print("REPL and data on", dev_repl)
        ser_repl = serial.Serial(dev_repl, baudrate=115200)
        ser_data = ser_repl
        usb_vcp_id = 0
    else:
        print("REPL on", dev_repl)
        print("data on", dev_data)
        print("USB VCP", usb_vcp_id)
        ser_repl = serial.Serial(dev_repl, baudrate=115200)
        ser_data = dev_data  # can't open this port until it exists

    if 0:
        for i in range(1000):
            print("======== TEST %04u ========" % i)
            read_test(ser_repl, ser_data, usb_vcp_id, 8000, 32)
            write_test(ser_repl, ser_data, usb_vcp_id, 8000, 32, True)
        return

    read_test_params = [
        (256, 128),
        (512, 64),
        (1024, 64),
        (2048, 64),
        (4096, 64),
        (8192, 64),
        (16384, 64),
    ]
    write_test_params = [(512, 16), (1024, 16)]  # for high speed mode due to lack of flow ctrl
    write_test_params = [
        (128, 32),
        (256, 16),
        (512, 16),
        (1024, 16),
        (2048, 16),
        (4096, 16),
        (8192, 64),
        (9999, 64),
    ]

    # ambiq
    # read_test_params = ((256, 512),)
    # write_test_params = ()
    # ambiq

    for bufsize, nbuf in read_test_params:
        t = read_test(ser_repl, ser_data, usb_vcp_id, bufsize, nbuf)
        if t > 8:
            break

    for bufsize, nbuf in write_test_params:
        t = write_test(ser_repl, ser_data, usb_vcp_id, bufsize, nbuf, True)
        if t > 8:
            break

    for bufsize, nbuf in write_test_params:
        t = write_test(ser_repl, ser_data, usb_vcp_id, bufsize, nbuf, False)
        if t > 8:
            break

    ser_repl.close()


if __name__ == "__main__":
    main()
