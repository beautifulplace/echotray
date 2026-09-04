#!/usr/bin/env bash
# Headless smoke test for EchoTray's GTK windows.
#
# Runs smoke_test.py inside the app venv, under a virtual X display (xvfb-run)
# when available, so it works on a headless box or a Wayland-only session
# without flashing windows on screen.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${ECHOTRAY_INSTALL_DIR:-$HOME/.local/share/echotray}"
APP_SRC="$INSTALL_DIR/app/src"
PY="$INSTALL_DIR/venv/bin/python"

if [ ! -x "$PY" ]; then
    echo "smoke-test: venv not found at $PY (run install.sh first)" >&2
    exit 1
fi

if command -v xvfb-run >/dev/null 2>&1; then
    exec xvfb-run -a "$PY" "$SCRIPT_DIR/smoke_test.py" "$APP_SRC"
else
    exec "$PY" "$SCRIPT_DIR/smoke_test.py" "$APP_SRC"
fi
