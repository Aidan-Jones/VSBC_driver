#!/usr/bin/env bash
# Setup of the VSBC tensile tester on a Raspberry Pi (Raspberry Pi OS Bookworm
# or newer, 64-bit, desktop). Starting from a Pi with only its OS installed:
#     sudo apt install -y git
#     git clone https://github.com/Aidan-Jones/VSBC_driver.git
#     cd VSBC_driver
#     bash scripts/install_pi.sh             # add --camera for the DIC camera environment
# It is safe to run again, for example after a "git pull".
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv"
CAMERA_VENV="$REPO/.venv-camera"
BIN="$HOME/.local/bin"
CAMERA=""
for arg in "$@"; do
    case "$arg" in
        --camera) CAMERA="y" ;;
        --no-camera) CAMERA="n" ;;
    esac
done

echo "==> System packages"
sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip python3-pyqt5 python3-pyqtgraph \
    python3-serial python3-numpy python3-matplotlib python3-lgpio curl

echo "==> Python environment in $VENV (it uses the apt packages above)"
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install -e "$REPO"
mkdir -p "$BIN"
ln -sf "$VENV/bin/vsbc" "$BIN/vsbc"
# ~/.local/bin only gets on the PATH at the next login (and not in every kind of
# terminal), so also link the command where every shell finds it right away.
sudo ln -sf "$VENV/bin/vsbc" /usr/local/bin/vsbc

echo "==> Serial port (Mega) and GPIO (load cell) permissions"
for group in dialout gpio; do
    if getent group "$group" >/dev/null && ! id -nG "$USER" | grep -qw "$group"; then
        sudo usermod -aG "$group" "$USER"
        echo "    Added $USER to the '$group' group: log out and back in once."
    fi
done

echo "==> arduino-cli (compiles and uploads the Mega firmware)"
if ! command -v arduino-cli >/dev/null 2>&1 && [ ! -x "$BIN/arduino-cli" ]; then
    curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR="$BIN" sh
fi
CLI="$(command -v arduino-cli || echo "$BIN/arduino-cli")"
if [ "$CLI" = "$BIN/arduino-cli" ]; then
    sudo ln -sf "$BIN/arduino-cli" /usr/local/bin/arduino-cli
fi
"$CLI" core update-index
"$CLI" core install arduino:avr

echo "==> Desktop launcher"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
sed -e "s|@VSBC@|$VENV/bin/vsbc|g" -e "s|@REPO@|$REPO|g" "$REPO/scripts/vsbc.desktop" > "$APPS/vsbc.desktop"

answer=""
if [ -f "$HOME/.config/autostart/vsbc.desktop" ]; then
    answer="y"   # chosen on an earlier run: refresh it
elif [ -t 0 ]; then
    read -r -p "Start the GUI full screen automatically at login? [y/N] " answer || answer=""
fi
if [[ "$answer" =~ ^[Yy] ]]; then
    mkdir -p "$HOME/.config/autostart"
    sed -e "s|@VSBC@ gui|@VSBC@ gui --fullscreen|" -e "s|@VSBC@|$VENV/bin/vsbc|g" -e "s|@REPO@|$REPO|g" \
        "$REPO/scripts/vsbc.desktop" > "$HOME/.config/autostart/vsbc.desktop"
    echo "    Added ~/.config/autostart/vsbc.desktop"
fi

echo "==> config.toml"
USER_CONFIG="$HOME/.config/vsbc/config.toml"
# <repo>/config.toml is read before ~/.config/vsbc/config.toml, so only create
# it when there is neither: a new one would hide the settings in the other.
if [ ! -f "$REPO/config.toml" ] && [ ! -f "$USER_CONFIG" ]; then
    cp "$REPO/config.example.toml" "$REPO/config.toml"
    echo "    Created config.toml: set the load cell's capacity_n in it."
fi
if [ -f "$REPO/config.toml" ] && [ -f "$USER_CONFIG" ]; then
    echo "    Both $REPO/config.toml and $USER_CONFIG exist: the app reads only $REPO/config.toml."
fi
# The load cell is read through the Mega. Installs before that change created
# config.toml with source = "pi", and "git pull" never touches config.toml.
for cfg in "$REPO/config.toml" "$USER_CONFIG"; do
    [ -f "$cfg" ] || continue
    if grep -Eq '^[[:space:]]*source[[:space:]]*=[[:space:]]*"pi"' "$cfg"; then
        sed -i -E 's/^([[:space:]]*source[[:space:]]*=[[:space:]]*)"pi"/\1"mega"/' "$cfg"
        echo "    $cfg: load-cell source changed from \"pi\" to \"mega\" (the HX711 is wired to the Mega)."
    fi
done

echo "==> DIC camera (Basler + pyvale)"
# The camera code runs in its own environment: pyvale needs PyQt6, opencv-python
# and NumPy 2, which must not mix with the app's apt PyQt5 / NumPy / matplotlib.
if [ -z "$CAMERA" ]; then
    if [ -x "$CAMERA_VENV/bin/python" ]; then
        CAMERA="y"   # installed on an earlier run: update it
    elif [ -t 0 ]; then
        read -r -p "Install the camera + DIC environment (about 1 GB, 10-20 min)? [y/N] " CAMERA || CAMERA=""
    fi
fi
REBOOT=""
if [[ "$CAMERA" =~ ^[Yy] ]]; then
    sudo apt-get install -y ffmpeg
    if [ ! -x "$CAMERA_VENV/bin/python" ]; then
        python3 -m venv "$CAMERA_VENV"
    fi
    "$CAMERA_VENV/bin/pip" install --upgrade pip
    "$CAMERA_VENV/bin/pip" install -r "$REPO/camera/requirements.txt"

    # Let pypylon open Basler USB cameras (vendor 2676) without root.
    RULE=/etc/udev/rules.d/69-basler-cameras.rules
    if [ ! -f "$RULE" ]; then
        echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="2676", MODE="0666"' | sudo tee "$RULE" >/dev/null
        sudo udevadm control --reload-rules && sudo udevadm trigger
        echo "    Added $RULE"
    fi

    # Basler recommends 1000 MB of USB buffer memory for USB 3 cameras (the default 16 MB drops frames).
    CMDLINE=/boot/firmware/cmdline.txt
    [ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt
    if [ -f "$CMDLINE" ] && ! grep -q "usbcore.usbfs_memory_mb" "$CMDLINE"; then
        sudo cp "$CMDLINE" "$CMDLINE.bak"
        sudo sed -i '1 s/$/ usbcore.usbfs_memory_mb=1000/' "$CMDLINE"
        echo "    Added usbcore.usbfs_memory_mb=1000 to $CMDLINE (backup: $CMDLINE.bak)"
        REBOOT="y"
    fi
    echo "    Camera environment: $CAMERA_VENV. Check it with:  vsbc camera check"
else
    echo "    Skipped (run again with --camera to add it)."
fi

echo
hash -r
if command -v vsbc >/dev/null 2>&1 && vsbc --version >/dev/null 2>&1; then
    echo "Done: the 'vsbc' command is ready ($(vsbc --version))."
else
    echo "Done, but the 'vsbc' command is not on the PATH of this terminal."
    echo "Use the full path instead:  $VENV/bin/vsbc"
fi
echo "Next:"
echo "  1. Log out and back in once: it gives your user permission to use the Mega's"
echo "     serial port and the GPIO pins."
echo "  2. Plug in the Mega and upload the firmware:   vsbc flash"
echo "  3. Check the load cell (wired to the Mega):    vsbc loadcell"
echo "  4. Start the app:                              vsbc"
echo "     (or 'VSBC Tensile Tester' in the desktop menu)"
if [[ "$CAMERA" =~ ^[Yy] ]]; then
    echo "  5. Check the camera, the disk and pyvale:      vsbc camera check"
fi
if [ -n "$REBOOT" ]; then
    echo
    echo "Reboot once for the USB camera memory setting to take effect."
fi
