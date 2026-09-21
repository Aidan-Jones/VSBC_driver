#!/usr/bin/env python3
"""
DM542T V4.0 stepper driver control from Raspberry Pi 5
Driver: Stepperonline DM542T V4.0
Motor: Stepperonline 23HS30-3004S (1.8°, 3.0A)

Driver settings:
  S2 selector: 5V   (factory default is 24V -- must be changed for 5V signals)
  SW1 OFF, SW2 OFF, SW3 ON  -> 2.37 A peak / 1.69 A RMS
  SW4 ON                    -> standstill current 90%
  SW5 OFF, SW6 ON, SW7 ON, SW8 ON -> 400 pulses/rev (matches STEPS_PER_REV)

Wiring (BCM numbering):
  PUL+  -> Pi 5V
  PUL-  -> GPIO 20
  DIR+  -> Pi 5V
  DIR-  -> GPIO 21

WARNING: the Pi's 3.3V GPIO cannot fully switch the driver's 5V opto inputs
(a GPIO "high" leaves ~1.7V across the input; DM542T V4.0 needs 0-0.5V for low
and 4.5-5V for high). Put an NPN transistor buffer on each line, as in the
Arduino setup, for reliable stepping.
"""

from gpiozero import DigitalOutputDevice
from time import sleep

STEP_PIN = 20
DIR_PIN = 21

step = DigitalOutputDevice(STEP_PIN)
direction = DigitalOutputDevice(DIR_PIN)

# Set this to match your driver's microstep DIP switches (SW5-8).
# DM542T V4.0: 400 = OFF ON ON ON, 1600 = OFF OFF ON ON, 3200 = ON ON OFF ON.
STEPS_PER_REV = 400

# Delay between pulse edges, in seconds. Smaller = faster rotation.
# DM542T needs only ~2.5us minimum pulse width, so software timing
# has plenty of margin -- this value mostly controls motor speed/torque feel.
# 0.0015 s -> at most ~333 steps/s (~50 RPM at 400 pulses/rev).
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

    finally:
        step.off()
        direction.off()
        step.close()
        direction.close()
