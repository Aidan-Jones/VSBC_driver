#!/usr/bin/env bash
# One-time setup of the VSBC tensile tester on a Raspberry Pi
# (Raspberry Pi OS Bookworm, 64-bit, desktop). From the repository folder:
#     bash scripts/install_pi.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv"
BIN="$HOME/.local/bin"

echo "==> System packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip python3-pyqt5 python3-pyqtgraph \
    python3-serial python3-numpy python3-matplotlib curl

echo "==> Python environment in $VENV (it uses the apt packages above)"
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install -e "$REPO"
mkdir -p "$BIN"
ln -sf "$VENV/bin/vsbc" "$BIN/vsbc"

echo "==> Serial port permission"
if ! id -nG "$USER" | grep -qw dialout; then
    sudo usermod -aG dialout "$USER"
    echo "    Added $USER to the 'dialout' group: log out and back in once."
fi

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

read -r -p "Start the GUI full screen automatically at login? [y/N] " answer
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
echo "  1. Log out and back in (serial permission, and ~/.local/bin on the PATH)."
echo "  2. Plug in the Mega and upload the firmware:   vsbc flash"
echo "  3. Start the app:                              vsbc"
echo "     (or 'VSBC Tensile Tester' in the desktop menu)"
