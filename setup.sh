#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
cd "$SCRIPT_DIR"

# Installation prefix: everything WhisperType owns lives here, per the user's
# request — the app code, its venv, its config, and the downloaded model.
INSTALL_DIR="$HOME/.local/share/whispertype"
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"

echo "=== WhisperType Setup ==="
echo ""
echo "Self-contained build: faster-whisper + CTranslate2, CPU-only (no GPU required)."
echo "The Whisper model is downloaded on first run and cached locally."
echo ""
echo "Everything is installed under: $INSTALL_DIR"
echo "Privileged input (Ctrl+V paste) is handled by the root-owned"
echo "whispertype-helperd daemon. Your user needs NO special groups."
echo ""

# Install system packages
echo "[1/4] Installing system packages..."
sudo apt install -y build-essential python3-dev python3-venv wl-clipboard libportaudio2 portaudio19-dev gir1.2-ayatanaappindicator3-0.1

# Build + install the privileged helper daemon (requires root)
echo ""
echo "[2/4] Installing the privileged helper daemon (whispertype-helperd)..."
sudo bash "$SCRIPT_DIR/install-helper.sh"

# Copy the app source into the install dir (idempotent)
echo ""
echo "[3/4] Installing app to $INSTALL_DIR ..."
mkdir -p "$APP_DIR"
cp -r "$SCRIPT_DIR/src" "$APP_DIR/src"
cp -r "$SCRIPT_DIR/assets" "$APP_DIR/assets"
cp "$SCRIPT_DIR/requirements.txt" "$APP_DIR/requirements.txt"
cp "$SCRIPT_DIR/.env.example" "$APP_DIR/.env.example"
echo "  Copied app to $APP_DIR"

# Check for an existing downloaded model and keep it (don't force re-download).
MODEL_ROOT="$INSTALL_DIR/models"
if [ -d "$MODEL_ROOT" ] && [ -n "$(ls -A "$MODEL_ROOT" 2>/dev/null)" ]; then
    echo "  Existing downloaded model found at $MODEL_ROOT — it will be kept and reused."
else
    echo "  No downloaded model yet — it will be fetched on first launch."
fi

# Create (or recreate) virtual environment inside the install dir
echo ""
echo "  Setting up Python virtual environment..."
if [ -d "$VENV_DIR" ]; then
    echo "    Removing existing venv for clean reinstall..."
    rm -rf "$VENV_DIR"
fi
/usr/bin/python3 -m venv --system-site-packages "$VENV_DIR"
echo "  Created $VENV_DIR"

# Install Python dependencies
echo ""
echo "  Installing Python dependencies..."
# --ignore-installed: the venv uses --system-site-packages, so pip sees the
# system's packages (protobuf, click, etc.) and tries to upgrade them, but they
# live outside the venv and can't be uninstalled — producing noisy "Attempting
# uninstall / Can't uninstall" warnings. --ignore-installed skips that entirely.
"$VENV_DIR/bin/pip" install --upgrade --ignore-installed pip
"$VENV_DIR/bin/pip" install --ignore-installed -r "$APP_DIR/requirements.txt"

# Set up .env config file (lives in the install dir, not the git clone)
echo ""
echo "[4/4] Setting up configuration..."
ENV_FILE="$INSTALL_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    echo "  .env already exists, keeping your settings."
else
    cp "$APP_DIR/.env.example" "$ENV_FILE"
    echo "  Created $ENV_FILE — edit it to customize settings."
fi

# Install .desktop file for app launcher
DESKTOP_FILE="$HOME/.local/share/applications/whispertype.desktop"
VENV_PYTHON="$VENV_DIR/bin/python"
APP_SCRIPT="$APP_DIR/src/app.py"
mkdir -p "$HOME/.local/share/applications"

# Use the dark icon by default (theme-aware variants available in assets/)
APP_ICON="$APP_DIR/assets/icon-dark.svg"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Name=WhisperType
Comment=Speech-to-text dictation by clicking the microphone
Exec=$VENV_PYTHON $APP_SCRIPT
Icon=$APP_ICON
Type=Application
Categories=Utility;Audio;
Terminal=false
StartupNotify=false
EOF
# GNOME requires the .desktop file to be marked executable/trusted before it
# will show/launch it from the app grid; otherwise clicking does nothing.
chmod +x "$DESKTOP_FILE"
echo "  Installed: $DESKTOP_FILE"

echo ""
echo "=== Setup complete ==="
echo ""
echo "The helper daemon is running. You can now launch 'WhisperType' from your"
echo "app menu. Everything is installed under: $INSTALL_DIR"
echo "  - app code:   $APP_DIR"
echo "  - venv:       $VENV_DIR"
echo "  - config:     $ENV_FILE"
echo "  - model:      $INSTALL_DIR/models/<size>/  (downloaded on first run)"
echo ""
echo "To run manually:"
echo "  $VENV_DIR/bin/python $APP_DIR/src/app.py"
echo ""
