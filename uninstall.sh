#!/usr/bin/env bash
# WhisperType — complete, silent uninstall.
#
# Removes EVERYTHING this version of WhisperType installs or creates, with no
# prompts. Clean slate model: no legacy handling, no memory of old versions.
# Reinstall anytime by running ./setup.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
DESKTOP_FILE="$HOME/.local/share/applications/whispertype.desktop"
INSTALL_DIR="$HOME/.local/share/whispertype"

echo "=== WhisperType uninstall ==="

# 1. Stop any running GUI
if pgrep -f "whispertype/app/src/app.py" >/dev/null 2>&1 || pgrep -f "src/app.py" >/dev/null 2>&1; then
    pkill -f "whispertype/app/src/app.py" 2>/dev/null || true
    pkill -f "src/app.py" 2>/dev/null || true
    echo "  Stopped running GUI."
fi

# 2. Stop and remove the helper daemon service (binary + unit file)
SERVICES=$(systemctl list-unit-files 2>/dev/null | awk '{print $1}' | grep -E '^whispertype-helperd\.service$' || true)
if [[ -n "$SERVICES" ]] || compgen -G "/etc/systemd/system/whispertype-helperd.service" >/dev/null 2>&1; then
    for svc in $SERVICES; do
        echo "  Stopping $svc ..."
        sudo systemctl stop "$svc" 2>/dev/null || true
        sudo systemctl disable "$svc" 2>/dev/null || true
        sudo systemctl reset-failed "$svc" 2>/dev/null || true
    done
    sudo rm -f /etc/systemd/system/whispertype-helperd.service 2>/dev/null || true
    sudo systemctl daemon-reload
    echo "  Removed helper daemon service."
fi
# Kill any stray daemon process
if pgrep -f "whispertype-helperd" >/dev/null 2>&1; then
    sudo pkill -f "whispertype-helperd" 2>/dev/null || true
fi
sudo rm -rf /usr/local/lib/whispertype 2>/dev/null || true

# 3. Remove socket + lock
sudo rm -f /run/whispertype.sock 2>/dev/null || true
rm -f /tmp/whispertype.lock 2>/dev/null || true

# 4. Remove desktop launcher
rm -f "$DESKTOP_FILE"
command -v update-desktop-database &>/dev/null && \
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

# 5. Remove the installed app (but ask about keeping the downloaded model).
MODEL_ROOT="$INSTALL_DIR/models"
if [[ -d "$INSTALL_DIR" ]]; then
    if [[ -d "$MODEL_ROOT" ]] && [[ -n "$(ls -A "$MODEL_ROOT" 2>/dev/null)" ]]; then
        read -r -p "  Keep the downloaded model (~/.local/share/whispertype/models) so reinstall skips re-downloading? [Y/n] " reply
        case "${reply,,}" in
            n|no)
                echo "  Removing install dir including the model..."
                rm -rf "$INSTALL_DIR"
                ;;
            *)
                echo "  Keeping the downloaded model."
                # Remove everything in the install dir EXCEPT the model.
                find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name models -exec rm -rf {} + 2>/dev/null || true
                ;;
        esac
    else
        echo "  No downloaded model found — removing install dir..."
        rm -rf "$INSTALL_DIR"
    fi
fi

echo ""
echo "=== Uninstall complete ==="
echo "  Removed: desktop launcher, install dir ($INSTALL_DIR),"
echo "           helper daemon service + binary, socket, lock."
echo "  Project folder is intact: $SCRIPT_DIR"
echo "  Run ./setup.sh to reinstall."
echo ""
