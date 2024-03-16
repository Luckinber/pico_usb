from machine import Pin, UART
from usbd import CDC
import usbd.device
import select
import sys
import time

# Create a list of all the ports we want to poll
ports = []

# Create a UART object and add it to the list
uart = UART(0, baudrate=115200, tx=Pin(16), rx=Pin(17))
uart.init(bits=8, parity=None, stop=1)
ports.append(uart)

# Create a CDC object and add it to the list
cdc1 = CDC()
cdc1.init(timeout=0)
ports.append(cdc1)

# # Create another CDC object and add it to the list
# cdc2 = CDC()
# cdc2.init(timeout=0)
# ports.append(cdc2)

# Add the standard input to the list
ports.append(sys.stdin)

# Initialise the USB driver(?) with the CDC objects
# usbd.device.get().init(cdc1, cdc2, builtin_drivers=True)
usbd.device.get().init(cdc1, builtin_drivers=True)

# Create a poll object and register all the ports
io = select.poll()
for port in ports:
	io.register(port, select.POLLIN)

led = Pin("LED", Pin.OUT)
led.off()

# Wait for the USB to be ready
time.sleep(1)

while True:
	for stream, event in io.ipoll(0):
		if stream in ports:
			led.on()
			data = stream.read(1)
			# If the data is a carriage return, add a newline
			if data == b'\r':
				data = b'\r\n'
			for port in ports:
				# If the port is the standard input, handle it differently
				if port == sys.stdin:
					if isinstance(data, str):
						print(data, end='')
					else:
						print(data.decode(), end='')
					continue
				port.write(data)
			led.off()