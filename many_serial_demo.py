from usbd import CDC
import usbd.device
import select
import time
 
ports = []
num_ports = 6
io = select.poll()

# Create a list of CDC objects
for i in range(num_ports):
	cdc = CDC()
	cdc.init(timeout=0)
	io.register(cdc, select.POLLIN)
	ports.append(cdc)

# Initialise the USB device with the list of CDC objects
usbd.device.get().init(*ports, builtin_drivers=True)

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