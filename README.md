# EchoTray

Local speech-to-text dictation for Linux (Wayland and X11). Click the tray microphone to
start recording, click again to stop - the transcript is pasted at your cursor.

[![version](https://img.shields.io/badge/version-2.4.0-blue)](CHANGELOG.md) [![python](https://img.shields.io/badge/language-python-blue)] [![license](https://img.shields.io/badge/license-MIT-blue)]

**Fully self-contained and offline.** The Whisper model is downloaded on first run
and cached locally. No external LLM, no cloud service, no GPU required - it runs on
CPU via int8 quantization. Works on both **AMD64 and ARM64** (CTranslate2 ships
official aarch64 wheels).

**Runs unprivileged.** A root-owned helper daemon (`echotray-helperd`) owns the
single privileged action needed - injecting Ctrl+V via `/dev/uinput`. It is written
in **Rust** (memory-safe, zero external crates, builds fully offline). The GUI runs
as your normal user with **no special groups** and talks to the daemon over a
root-owned Unix socket (`/run/echotray.sock`). See **Architecture** below.

This is an adaptation of [Humeruzz/whisper-dictation](https://github.com/Humeruzz/whisper-dictation)
(MIT), modified to be self-contained and click-driven: the LLM-cleanup layer and the
global hotkey were removed, and privileged input was moved into a separate
root-owned daemon. The design - including the click-to-talk interaction and the
entirely local, offline architecture - is inspired by
[woheller69/whisperIMEplus](https://github.com/woheller69/whisperIMEplus).

## Contents

- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Desktop Compatibility](#desktop-compatibility)
- [Setup](#setup)
- [Configuration](#configuration)
- [Usage](#usage)
- [Updating](#updating)
- [Tray Icon Menu](#tray-icon-menu)
- [Clipboard behavior](#clipboard-behavior)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## How It Works

1. EchoTray lives in the tray as a small microphone icon (green = ready).
2. Click it once → starts recording (icon turns **red**).
3. Click it again → recording stops and the audio is transcribed with
   **faster-whisper** (CTranslate2), using Silero VAD to filter silence.
4. The transcript is copied to the clipboard (`wl-copy` on Wayland, `xclip` on
   X11), then the GUI asks the helper daemon to inject a Ctrl+V via
   `/dev/uinput` - so it lands in whatever field has focus (e.g. a Firefox
   search bar).

## Architecture

EchoTray is split into two processes to minimize the privileges needed by the GUI:

```
┌─────────────────────────────┐        ┌─────────────────────────────┐
│  echotray (GUI)             │        │  echotray-helperd (root)    │
│  -- runs as your user       │        │  -- runs via systemd (root) │
│  -- NO special groups       │        │  -- Rust, memory-safe       │
│                             │  UNIX  │  -- owns /dev/uinput        │
│  · mic recording            │ socket │                             │
│  · Whisper transcription    │───────▶│  · injects Ctrl+V (paste)   │
│  · clipboard (wl-copy/xclip)│        │                             │
│  · tray icon / notifications│        │                             │
└─────────────────────────────┘        └─────────────────────────────┘
```

The daemon is **paste-only** - it has no hotkey and does not read the keyboard. The
trigger is the tray icon in the GUI. This means no hotkey keystrokes ever leak into
the app you're typing in.

**Security model:** `/dev/uinput` is never exposed to the unprivileged GUI or to
arbitrary apps. Only the root daemon can open it. The GUI's only interface is the
root-owned Unix socket, and the daemon rejects connections from root processes (via
`SO_PEERCRED`), so a root-owned malicious service can't be tricked by the socket.

This is a deliberately clean split: the small, security-critical daemon is written in
**memory-safe Rust** (zero external crates, builds fully offline) and is the only
privileged component; everything else (the large Python/ML surface) runs with your
normal user's privileges.

## Requirements

- Linux with Wayland or X11 (tested on GNOME, KDE Plasma, and XFCE)
- Python 3.10+
- A microphone
- `systemd` (for the helper daemon service)
- A desktop with AppIndicator tray support (see **Desktop Compatibility** below)

## Desktop Compatibility

EchoTray needs **AppIndicator** tray support. It works on both **Wayland and
X11**, and is not tied to any one desktop:

| Desktop | Tray icon | Left-click | Right-click | Notes |
|---------|-----------|------------|-------------|-------|
| **GNOME** | Needs the AppIndicator extension enabled | Opens the menu | Opens the menu | Use **Start/Stop recording** from the tray menu. Extension is installed by default on Ubuntu: `gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com` |
| **KDE Plasma** | Works out of the box | Opens the menu | Opens the menu | Use **Start/Stop recording** from the tray menu |
| **XFCE** | Works out of the box | Opens the menu | Opens the menu | Use **Start/Stop recording** from the tray menu |

## Setup

```bash
git clone https://github.com/beautifulplace/echotray.git
cd echotray
./install.sh
```

The setup script:
- Installs system packages (`wl-clipboard`, PortAudio, AppIndicator GIR bindings, `rustc`/`cargo`)
- Builds and installs the helper daemon (`echotray-helperd`) as a systemd service
  that runs as root
- Installs the app into `~/.local/share/echotray/` (app code, its venv, config,
  and the downloaded model), creating a Python venv and installing dependencies
- Installs a `.desktop` launcher
- Installs an `echotray` command (a wrapper symlinked into `~/.local/bin`) that
  launches the app and provides the update/maintenance subcommands (see
  **Updating** below)

Everything EchoTray owns lives under **`~/.local/share/echotray/`**:
- `app/` - the app source
- `venv/` - the Python virtual environment
- `.env` - your configuration
- `models/` - the downloaded Whisper model (on first run)

**No `input` group membership or `uinput` udev rule is required** - the helper daemon
handles all privileged input as root.

## Configuration

Your configuration lives at **`~/.local/share/echotray/.env`** (created on setup
from the template). Edit it to your preferences:

```bash
nano ~/.local/share/echotray/.env
```

### Key settings

| Setting | Default | Description |
|---------|---------|-------------|
| `MODEL_SIZE` | `small` | Whisper model: tiny, base, small, medium, large-v3 |
| `COMPUTE_TYPE` | `int8` | `int8` = fastest on CPU (recommended; float16 not supported) |
| `WHISPER_LANGUAGE` | `en` | Language code, or empty for auto-detect |
| `PASTE_DELAY_MS` | `100` | Delay between clipboard copy and Ctrl+V (increase if paste is blank) |
| `MODEL_DIR` | `~/.local/share/echotray/models` | Where the Whisper model is stored |

## Usage

**From the app menu:** Search for "EchoTray" in Activities / app launcher.

**From the terminal:**
```bash
echotray
```

(`install.sh` installs an `echotray` command - a wrapper at
`~/.local/share/echotray/echotray` symlinked into `~/.local/bin`. If `~/.local/bin`
isn't on your PATH, run it as
`~/.local/share/echotray/echotray` instead.)

1. Wait for the "Ready" notification (first run downloads the Whisper model)
2. Click the tray microphone to start recording (turns **red**, "Recording - Speak now..." notification)
3. Put the cursor where you want the text (e.g. a search bar)
4. Speak
5. Click it again to stop; the transcript is pasted at your cursor
   (turns **amber** while transcribing, then shows a **"Transcribed: N words in X.Xs"**
   notification when done)

The tray icon's color shows the state: **green** = ready, **red** = recording,
**amber** = transcribing.

## Updating

EchoTray checks for updates automatically when you open the **About** window.
If a newer version is available, an orange pill shows the new version number
with **Update** and **Ignore version** buttons.

- **Update** downloads and installs the new version in place, then prompts you
  to restart.
- **Ignore version** hides the update prompt until a newer version appears.

You can also update from the command line with the `echotray` command:

```bash
echotray check              # show the latest available version
echotray upgrade            # install the latest version
echotray upgrade --sudo     # full install (for privileged releases)
echotray ignore <version>   # hide the update prompt for a version
```

Most updates install without extra privileges. A release that changes the
helper daemon or adds system packages is marked as needing a privileged
install; the About window then shows a copyable `echotray upgrade --sudo`
command instead of the Update button, and the CLI stops with a clear message if
you run `echotray upgrade` without `--sudo`. After a command-line upgrade, a
running EchoTray instance is restarted automatically.

## Tray Icon Menu

Right-clicking the tray icon opens a menu with:
- **Status** - shows the current app state (Idle / Recording / Transcribing)
- **Start/Stop recording** - same as left-clicking the icon
- **Setup...** - opens a window to install the Whisper model (first run) or
  change the model size (tiny/base/small/medium/large-v3). A green light means
  a model is loaded; red means none. Below the model section are simple
  selectors for the dictation language, the paste delay, and the max recording
  length.
- **About** - app name, version, and description. Opening it also checks for
  updates (see **Updating** above).
- **Quit** - exits the app

## Clipboard behavior

EchoTray pastes by copying the transcribed text to the clipboard and then
injecting Ctrl+V. This **overwrites whatever was previously on your clipboard**.
If you want to keep a history of everything you copy, install a clipboard
manager for your desktop (e.g. a GNOME clipboard-indicator extension, or
cliphist on wlroots-based compositors).

## Testing

The project ships an **offline, dependency-free unit test suite** for the pure
model-cache / download / transcription logic in `src/whisper.py` and the
update-checking logic in `src/updater.py`. It needs no audio device, no GPU,
and no network — the heavy ML deps (`faster-whisper`, `sounddevice`, `numpy`)
are imported lazily, so nothing extra is pulled in just to import the module.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"   # pytest + ruff
.venv/bin/python -m pytest          # all tests pass offline
```

Configuration is exercised directly, including the guard against a malformed
`.env` value crashing the app at import (it falls back to the default with a
warning instead). The updater tests cover version parsing/comparison, the
ignored-version gate, sudo-required release detection, and the safe-restart
logic (including the guard against signaling a reused PID).

## Troubleshooting

| Error | Fix |
|-------|-----|
| `echotray-helperd is not running` | `sudo systemctl start echotray-helperd`, or re-run `./install.sh` |
| No audio input device found | Check mic is connected, PipeWire is running |
| Text not appearing at cursor | Text is on your clipboard - paste manually with Ctrl+V |
| No tray icon | `gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com` |

## License

MIT License - see [LICENSE](LICENSE).

This project is an adaptation of
[Humeruzz/whisper-dictation](https://github.com/Humeruzz/whisper-dictation) (MIT),
and uses:
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT)
- [OpenAI Whisper](https://github.com/openai/whisper) (MIT)
- [Silero VAD](https://github.com/snakers4/silero-vad) (MIT)

### Acknowledgements

- **whisper-dictation** (MIT, © 2026 Humeruzz) - the base this project is adapted
  from. The GUI/transcription core (tray app skeleton, `AudioRecorder`, and
  `transcribe_audio`) is derived from it and is used under the MIT license.
- **whisperIMEplus** by woheller69 - the inspiration for the design: the simple
  click-to-talk interaction and the entirely local, offline architecture. No code
  is used from it.
