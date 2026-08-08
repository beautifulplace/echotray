#!/usr/bin/env bash
# Build and install the WhisperType privileged helper daemon (Rust).
# Must be run with sudo/root.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
CARGO_DIR="$SCRIPT_DIR/src/helper"
SERVICE="$CARGO_DIR/whispertype-helperd.service"
BIN_DIR="/usr/local/lib/whispertype"
BIN="$BIN_DIR/whispertype-helperd"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run this as root: sudo $0" >&2
    exit 1
fi

echo "=== Installing WhisperType helper daemon (Rust) ==="

# 1. Ensure cargo/rustc is available
echo "  Step 1/4: Ensuring Rust toolchain (rustc, cargo)..."
if ! command -v cargo >/dev/null 2>&1 || ! command -v rustc >/dev/null 2>&1; then
    echo "    rustc/cargo not found — installing via apt (this downloads ~100MB,"
    echo "    so it can take a while; apt shows a progress bar below)..."
    apt-get update
    apt-get install -y rustc cargo
else
    echo "    rustc/cargo already installed."
fi

# 2. Build the daemon (offline — zero external crates)
echo "  Step 2/4: Building whispertype-helperd (release)..."
(cd "$CARGO_DIR" && cargo build --release)
mkdir -p "$BIN_DIR"
install -m 755 "$CARGO_DIR/target/release/whispertype-helperd" "$BIN"
echo "    Built $BIN"

# 3. Install the systemd service
echo "  Step 3/4: Installing systemd service..."
install -m 644 "$SERVICE" /etc/systemd/system/whispertype-helperd.service
systemctl daemon-reload

# 4. Enable + start (or restart if already running)
echo "  Step 4/4: Enabling and starting service..."
systemctl enable whispertype-helperd.service
systemctl restart whispertype-helperd.service

echo ""
echo "=== Done ==="
echo "Helper daemon installed (Rust, memory-safe). Verify with:"
echo "  systemctl status whispertype-helperd"
echo "The GUI connects over /run/whispertype.sock as an unprivileged user."
