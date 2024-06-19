import usb.device
from usb.device.cdc import CDCInterface
from machine import Pin, UART
import sys
import select
import time

# Create a list of all the ports we want to poll
ports = []

# Create a UART object and add it to the list
uart = UART(0, baudrate=115200, tx=Pin(16), rx=Pin(17))
uart.init(bits=8, parity=None, stop=1)
ports.append(uart)

# Create a CDC object and add it to the list
cdc = CDCInterface()
cdc.init(timeout=0)
ports.append(cdc)
# Initialise the USB driver with the CDC object
usb.device.get().init(cdc, builtin_driver=True)

# Add the standard input to the list
ports.append(sys.stdin)

# Create a poll object and register all the ports
io = select.poll()
for port in ports:
	io.register(port, select.POLLIN)

led = Pin("LED", Pin.OUT)
led.off()

# Wait for the USB to be ready
while not cdc.is_open():
    time.sleep_ms(100)

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