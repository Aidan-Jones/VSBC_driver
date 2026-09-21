#!/usr/bin/env python3
"""
DM542T stepper driver control from Raspberry Pi 5
Motor: Stepperonline 23HS30-3004S (1.8°, 3.0A)

Wiring (BCM numbering):
  PUL+  -> Pi 5V
  PUL-  -> GPIO 20
  DIR+  -> Pi 5V
  DIR-  -> GPIO 21
"""

from gpiozero import DigitalOutputDevice
from time import sleep

STEP_PIN = 20
DIR_PIN = 21

step = DigitalOutputDevice(STEP_PIN)
direction = DigitalOutputDevice(DIR_PIN)

# Set this to match your driver's microstep DIP switches (SW5-8).
# e.g. 400 for half-step, 1600 for 1/8 microstep, 3200 for 1/16, etc.
STEPS_PER_REV = 400

# Delay between pulse edges, in seconds. Smaller = faster rotation.
# DM542T needs only ~2.5us minimum pulse width, so software timing
# has plenty of margin -- this value mostly controls motor speed/torque feel.
PULSE_DELAY = 0.0015


def rotate(steps, clockwise=True, delay=PULSE_DELAY):
    """Rotate the motor a given number of steps."""
    direction.value = clockwise
    sleep(0.001)  # small settle time after changing direction, before first pulse
    for _ in range(steps):
        step.on()
        sleep(delay)
        step.off()
        sleep(delay)


if __name__ == "__main__":
    try:
        print("Rotating one full revolution clockwise...")
        rotate(STEPS_PER_REV, clockwise=True)
        sleep(0.5)

        print("Rotating one full revolution counter-clockwise...")
        rotate(STEPS_PER_REV, clockwise=False)

    except KeyboardInterrupt:
        print("Stopped by user.")
