#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
cd "$SCRIPT_DIR"

# Installation prefix: everything EchoTray owns lives here, per the user's
# request - the app code, its venv, its config, and the downloaded model.
INSTALL_DIR="$HOME/.local/share/echotray"
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"

echo "=== EchoTray Setup ==="
echo ""
echo "Self-contained build: faster-whisper + CTranslate2, CPU-only (no GPU required)."
echo "The Whisper model is downloaded on first run and cached locally."
echo ""
echo "Everything is installed under: $INSTALL_DIR"
echo "Privileged input (Ctrl+V paste) is handled by the root-owned"
echo "echotray-helperd daemon. Your user needs NO special groups."
echo ""

# Install system packages only if any are missing. dpkg -s is fast and avoids
# a heavy apt round-trip when everything is already installed.
_REQUIRED_PACKAGES=(
    build-essential
    python3-dev
    python3-venv
    wl-clipboard
    libportaudio2
    portaudio19-dev
    gir1.2-ayatanaappindicator3-0.1
    libnotify-bin
)

_missing_packages() {
    local missing=""
    for pkg in "${_REQUIRED_PACKAGES[@]}"; do
        if ! dpkg -s "$pkg" >/dev/null 2>&1; then
            missing="$missing $pkg"
        fi
    done
    echo "$missing"
}

MISSING=$(_missing_packages)
if [ -n "$MISSING" ]; then
    echo "[1/4] Installing missing system packages...$MISSING"
    sudo apt install -y $MISSING
else
    echo "[1/4] All required system packages are already installed - skipping apt."
fi

# Build + install the privileged helper daemon (requires root)
echo ""
echo "[2/4] Ensuring the privileged helper daemon (echotray-helperd)..."
sudo bash "$SCRIPT_DIR/helper_install.sh"

# Copy the app source into the install dir (idempotent)
echo ""
echo "[3/4] Installing app to $INSTALL_DIR ..."
mkdir -p "$APP_DIR"
# Remove any prior copy first so `cp -r src` doesn't nest into src/src/ on a
# re-run (cp -r into an existing dir creates src/src/...).
rm -rf "$APP_DIR/src" "$APP_DIR/assets"
cp -r "$SCRIPT_DIR/src" "$APP_DIR/src"
cp -r "$SCRIPT_DIR/assets" "$APP_DIR/assets"
cp "$SCRIPT_DIR/requirements.txt" "$APP_DIR/requirements.txt"
cp "$SCRIPT_DIR/.env.example" "$APP_DIR/.env.example"
echo "  Copied app to $APP_DIR"

# Check for an existing downloaded model and keep it (don't force re-download).
MODEL_ROOT="$INSTALL_DIR/models"
if [ -d "$MODEL_ROOT" ] && [ -n "$(ls -A "$MODEL_ROOT" 2>/dev/null)" ]; then
    echo "  Existing downloaded model found at $MODEL_ROOT - it will be kept and reused."
else
    echo "  No downloaded model yet - it will be fetched on first launch."
fi

# Install uv (fast Rust-based package installer). This is what makes the
# dependency install fast — plain pip resolves and downloads sequentially,
# while uv resolves in parallel and reuses a warm cache.
echo ""
echo "  Installing uv (fast package installer)..."
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# Ensure uv is on PATH for this script (install.sh puts it in ~/.local/bin or ~/.cargo/bin)
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
uv --version

# Create (or recreate) virtual environment inside the install dir.
# --system-site-packages keeps the system's GTK bindings (python3-gi,
# python3-cairo) so we don't have to build PyGObject/pycairo from source.
echo ""
echo "  Setting up Python virtual environment..."
if [ -d "$VENV_DIR" ]; then
    echo "    Removing existing venv for clean reinstall..."
    rm -rf "$VENV_DIR"
fi
uv venv --system-site-packages --python /usr/bin/python3 "$VENV_DIR"
echo "  Created $VENV_DIR"

# Install Python dependencies with uv (fast, parallel resolution).
echo ""
echo "  Installing Python dependencies..."
uv pip install --python "$VENV_DIR/bin/python" -r "$APP_DIR/requirements.txt"

# Set up .env config file (lives in the install dir, not the git clone)
echo ""
echo "[4/4] Setting up configuration..."
ENV_FILE="$INSTALL_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    echo "  .env already exists, keeping your settings."
else
    cp "$APP_DIR/.env.example" "$ENV_FILE"
    echo "  Created $ENV_FILE - edit it to customize settings."
fi

# Install .desktop file for app launcher
DESKTOP_FILE="$HOME/.local/share/applications/echotray.desktop"
VENV_PYTHON="$VENV_DIR/bin/python"
APP_SCRIPT="$APP_DIR/src/app.py"
mkdir -p "$HOME/.local/share/applications"

# Use the same icon as the tray (icon-idle.svg) so the desktop launcher and
# the tray match.
APP_ICON="$APP_DIR/assets/icon-idle.svg"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Name=EchoTray
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

# Create a simple CLI wrapper so the app can be run with just 'echotray'
# instead of the full venv python path. It launches the app detached (new
# session, backgrounded) so EchoTray keeps running after the terminal closes.
WRAPPER="$INSTALL_DIR/echotray"
cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
# EchoTray launcher - runs the app detached so it survives closing the terminal.
setsid "$VENV_PYTHON" "$APP_SCRIPT" "\$@" </dev/null >>/tmp/echotray.log 2>&1 &
EOF
chmod +x "$WRAPPER"
echo "  Installed CLI wrapper: $WRAPPER"

# Symlink the wrapper into ~/.local/bin so 'echotray' works from the terminal.
mkdir -p "$HOME/.local/bin"
ln -sf "$WRAPPER" "$HOME/.local/bin/echotray"
echo "  Symlinked: $HOME/.local/bin/echotray"

echo ""
echo "=== Setup complete ==="
echo ""
echo "The helper daemon is running. You can now launch 'EchoTray' from your"
echo "app menu. Everything is installed under: $INSTALL_DIR"
echo "  - app code:   $APP_DIR"
echo "  - venv:       $VENV_DIR"
echo "  - config:     $ENV_FILE"
echo "  - model:      $INSTALL_DIR/models/<size>/  (downloaded on first run)"
echo ""
echo "To run from the terminal, type 'echotray' (a CLI wrapper is installed at"
echo "  $WRAPPER and symlinked into ~/.local/bin)."
echo "Or run the app directly:"
echo "  $VENV_DIR/bin/python $APP_DIR/src/app.py"
echo ""
