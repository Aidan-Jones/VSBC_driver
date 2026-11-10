"""The Machine and System tabs: containers that group the existing panels by task.

Machine   Jog · Position reference · Calibration (folded) · Service (folded)
System    Display · Console · Firmware, as tabs of their own

The panels inside (JogPanel, CalibratePanel, UnitsPanel, ConsolePanel, FirmwarePanel) stay in the
window's `panels` list, so enabling, units and the theme reach them as before.
"""

from __future__ import annotations

from vsbc.gui.common import FlowTabs, Qt, QtWidgets, Section


def scroll(widget) -> QtWidgets.QScrollArea:
    """Scrolls up and down when the screen is too short; never sideways."""
    area = QtWidgets.QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(widget)
    return area


class MachinePanel(QtWidgets.QWidget):
    """Everything about the machine itself: moving it, its position reference, its calibration."""

    def __init__(self, win):
        super().__init__()
        jog, cal = win.jog_panel, win.calibrate_panel
        self.jog_section = Section("Jog", jog, key="machine.jog")
        self.reference_section = Section("Position reference", jog.reference_box, key="machine.reference")
        self.calibration_section = Section("Calibration and limits", cal, key="machine.calibration",
                                           expanded=False, framed=False)
        self.service_section = Section("Service: one motor only", jog.service_box, key="machine.service",
                                       expanded=False)
        self.service_section.set_summary("both motors")
        layout = QtWidgets.QVBoxLayout(self)
        for section in (self.jog_section, self.reference_section, self.calibration_section, self.service_section):
            layout.addWidget(section)
        layout.addStretch(1)
        jog.motors.currentTextChanged.connect(
            lambda text: self.service_section.set_summary("both motors" if text.startswith("Both") else text))


class SystemPanel(QtWidgets.QWidget):
    """Set once, or for troubleshooting: the theme and units, the serial console, the firmware."""

    def __init__(self, win):
        super().__init__()
        self.tabs = FlowTabs()
        self.tabs.addTab(scroll(win.units_panel), "Display")
        self.tabs.addTab(win.console, "Console")
        self.tabs.addTab(scroll(win.firmware_panel), "Firmware")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)
