# MicroPython USB MIDI example
# MIT license; Copyright (c) 2023 Angus Gratton
import usbd
import time


class MIDIExample(usbd.MIDIUSB):
    def on_note_on(self, channel, pitch, vel):
        print(f"RX Note On channel {channel} pitch {pitch} velocity {vel}")

    def on_note_off(self, channel, pitch, vel):
        print(f"RX Note Off channel {channel} pitch {pitch} velocity {vel}")

    def on_control_change(self, channel, controller, value):
        print(f"RX Control channel {channel} controller {controller} value {value}")


m = MIDIExample()
usbd.device.get().init(m)

print("Waiting for USB host to configure the interface...")

while not m.is_open():
    time.sleep_ms(100)

print("Starting MIDI loop...")

control_val = 0
channel = 0


while m.is_open():
    time.sleep(1)
    m.note_on(channel, 60)
    time.sleep(0.5)
    m.note_off(channel, 60)
    time.sleep(1)
    m.control_change(channel, 64, control_val)
    control_val += 1
    if control_val == 0x7F:
        control_val = 0
    time.sleep(1)
