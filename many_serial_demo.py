import usb.device
from usb.device.cdc import CDCInterface
import select
import time

ports = []
num_ports = 6
io = select.poll()

# Create a list of CDC objects
for i in range(num_ports):
	cdc = CDCInterface()
	cdc.init(timeout=0)
	io.register(cdc, select.POLLIN)
	ports.append(cdc)

# Initialise the USB device with the list of CDC objects
usb.device.get().init(*ports, builtin_driver=True)

# Wait for all ports to be opened
while not all(port.is_open() for port in ports):
	time.sleep_ms(100)

while True:
	# Poll for data on all ports
	for stream, event in io.ipoll(0):
		# Read data from the port and write it to all other ports
		if stream in ports:
			data = stream.read(256)
			if data == b'\r':
				data = b'\r\n'
			for port in ports:
				port.write(data)