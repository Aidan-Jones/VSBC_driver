#!/usr/bin/env bash
# Setup of the VSBC tensile tester on a Raspberry Pi (Raspberry Pi OS Bookworm
# or newer, 64-bit, desktop). Starting from a Pi with only its OS installed:
#     sudo apt install -y git
#     git clone https://github.com/Aidan-Jones/VSBC_driver.git
#     cd VSBC_driver
#     bash scripts/install_pi.sh
# It is safe to run again, for example after a "git pull".
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv"
BIN="$HOME/.local/bin"

echo "==> System packages"
sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip python3-pyqt5 python3-pyqtgraph \
    python3-serial python3-numpy python3-matplotlib python3-lgpio curl

echo "==> Python environment in $VENV (it uses the apt packages above)"
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install -e "$REPO"
mkdir -p "$BIN"
ln -sf "$VENV/bin/vsbc" "$BIN/vsbc"

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

if [ ! -f "$REPO/config.toml" ]; then
    cp "$REPO/config.example.toml" "$REPO/config.toml"
    echo "    Created config.toml: set the load cell's capacity_n in it."
fi

echo
echo "Done. Next:"
echo "  1. Log out and back in (serial and GPIO permissions, and ~/.local/bin on the PATH)."
echo "  2. Plug in the Mega and upload the firmware:   vsbc flash"
echo "  3. Check the load cell wired to the Pi:        vsbc loadcell"
echo "  4. Start the app:                              vsbc"
echo "     (or 'VSBC Tensile Tester' in the desktop menu)"
