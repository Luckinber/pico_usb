from usbd import CDC
import usbd.device
import os
import time

cdc = CDC()
cdc.init(timeout=0)  # zero timeout makes this non-blocking, suitable for os.dupterm()

# pass builtin_drivers=True so that we get the static USB-CDC alongside,
# if it's available.
usbd.device.get().init(cdc, builtin_drivers=True)

print("Waiting for USB host to configure the interface...")

# wait for host enumerate as a CDC device...
while not cdc.is_open():
    time.sleep_ms(100)

print("Waiting for CDC port to open...")

# cdc.is_open() returns true after enumeration finishes.
# cdc.dtr is not set until the host opens the port and asserts DTR
while not (cdc.is_open() and cdc.dtr):
    time.sleep_ms(20)

print("CDC port is open, duplicating REPL...")

old_term = os.dupterm(cdc)

print("Welcome to REPL, running on CDC interface implemented in Python.")
