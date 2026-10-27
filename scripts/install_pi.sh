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
