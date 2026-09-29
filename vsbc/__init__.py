"""VSBC tensile tester host software.

Runs on the Raspberry Pi (or a PC) and drives the Arduino Mega running
firmware/vsbc_firmware over USB serial. See docs/protocol.md for the protocol.
"""

__version__ = "2.0.0"

# The firmware this package was written against. PROTOCOL_VERSION must match
# exactly; a different FIRMWARE_VERSION only produces a warning.
FIRMWARE_NAME = "vsbc_firmware"
FIRMWARE_VERSION = "2.0.0"
PROTOCOL_VERSION = 1
