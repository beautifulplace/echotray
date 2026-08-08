# Changelog

## [1.0.0] - 2026-08-08

First public release. WhisperType is a self-contained, offline speech-to-text
dictation app for Linux (Wayland). Click the tray microphone to record, click
again to stop — the transcript is pasted at your cursor.

- Tray-only, one-click dictation (round colored mic icons: green/red/amber)
- Fully offline: faster-whisper + CTranslate2, CPU-only (int8), no GPU, no cloud
- Works on AMD64 and ARM64
- Runs unprivileged: a root-owned Rust helper daemon (`whispertype-helperd`,
  zero external crates) owns the single privileged action — injecting Ctrl+V
- Trailing space after every dictation so you can keep typing
- Clean-slate install/uninstall; model kept on reinstall

## [4.8.1] - 2026-08-07

### Changed
- **Reverted the forced period.** Whisper's natural punctuation is kept (it
  occasionally omits a period, but forcing one can produce wrong punctuation).
  The trailing space is still always added.

## [4.8.0] - 2026-08-07

### Fixed
- **About dialog logo no longer fills the screen.** The logo (a 512px SVG) is now
  scaled down to 128px, so the About text is readable on small screens.
- **Every dictation now ends with a period and a space.** Whisper sometimes
  omitted the period; the app now adds one if the text doesn't already end with
  `.`, `!`, or `?`, then always appends the trailing space.

## [4.7.0] - 2026-08-07

### Changed
- **Removed the NVIDIA GPU option.** WhisperType now always runs on CPU (int8).
  The `DEVICE` setting and GPU auto-detection were removed — it works identically
  on any machine, including ones with an NVIDIA GPU where CUDA/float16 caused
  problems. `float16` is no longer supported (it's forced to int8 if set).

## [4.6.1] - 2026-08-07

### Changed
- **About dialog now shows the app icon** (the desktop-launcher `icon-dark.svg`)
  as its logo, instead of GTK's placeholder circle-with-a-line-through icon.
  This is a placeholder until a dedicated logo is designed.

## [4.6.0] - 2026-08-07

### Changed
- **Trailing space after each dictation (always on).** Every transcription ends
  with a space, so you can keep typing the next sentence without pressing the
  space bar, and consecutive dictations don't run together. This is now the
  fixed behavior — no toggle.

## [4.5.2] - 2026-08-07

### Fixed
- **Setup no longer looks stuck while installing the Rust toolchain.** The
  `apt-get update`/`install` for rustc/cargo used `-qq`, which suppressed all
  output — so the ~100MB download appeared frozen. It now shows apt's normal
  progress bar and a note explaining the download.

## [4.5.1] - 2026-08-07

### Fixed
- **Silenced pip "Attempting uninstall / Can't uninstall" warnings during setup.**
  The venv uses `--system-site-packages`, so pip saw the system's `protobuf`,
  `click`, etc. and tried to upgrade them, but they live outside the venv and
  can't be uninstalled. Setup now passes `--ignore-installed` to pip, which
  skips that entirely.

## [4.5.0] - 2026-08-07

### Fixed
- **About dialog no longer freezes the app.** It used `dialog.run()`, which blocks
  the GTK main loop — so while About was open, the tray icon couldn't stop
  recording and the app couldn't quit. It now shows non-modally (`show` +
  `present`), so the tray stays live and the app remains fully usable.
- **Removed the "(If left-click opens the menu...)" line** from the About text.

## [4.4.0] - 2026-08-07

### Changed
- **Tray-only app.** Removed the floating window entirely (GNOME's focus rules
  made a non-focus-stealing window unreliable). WhisperType now lives in the tray
  only: left-click the tray icon toggles recording (one-click), and the tray menu
  has Start/Stop recording.
- **Tray icons are now bold round buttons** (solid green/red/amber circle with a
  white mic), matching the button style — far more visible than the previous
  thin-line mic at small tray size.

## [4.3.0] - 2026-08-07

### Fixed
- **Clicking the record button no longer steals keyboard focus.** `accept_focus`
  alone wasn't honored by GNOME for a plain window. The button window now uses
  the `DOCK` window type hint (the canonical way to make an always-on-top
  desktop overlay that does not take focus). Combined with the previous
  accept_focus(False)/focus_on_map(False), the document keeps focus so the
  injected Ctrl+V lands at the cursor — restoring the behavior from the earlier
  frameless-popup iteration that worked.

## [4.2.0] - 2026-08-07

### Fixed
- **Paste now lands where the cursor is, not in the clipboard.** Clicking the
  record-button window was stealing keyboard focus from the document, so the
  injected Ctrl+V went to the button window instead of the page. The window now
  sets `accept_focus(False)` + `focus_on_map(False)`, so the document keeps
  focus while the button still receives mouse clicks and the paste lands in the
  page.

### Changed
- **The record button turns amber while transcribing.** After you click to stop,
  the button shows yellow/amber during the ~3s STT step, then green again when
  done — so it's clear the app is working, not stuck.

## [4.1.0] - 2026-08-07

### Fixed
- **Close (X) button now on the right side** of the title bar (the decoration
  layout string had it on the left).
- **Stop-recording via the button is robust.** The stop path could previously
  get stuck in the TRANSCRIBING state if stopping the recorder failed; it now
  guards against a missing recorder and always recovers to Idle so the next
  click works. Recording start also recovers to Idle on any error.

### Changed
- **The record button no longer opens automatically at startup.** It's hidden
  until the user opens it from the tray icon (left-click) or the tray menu
  ("Show button"). The tray is the entry point again.

## [4.0.0] - 2026-08-07

### Changed
- **Proper window frame for the floating mic button.** Replaced the fragile
  frameless window with a real titled window that has a header bar. This gives a
  native, compositor-draggable title bar (moving works on Wayland, where a
  programmatic `window.move()` is a no-op) and a native close button (X) that
  hides the button back to the tray instead of quitting. `set_keep_above(True)`
  on a normal titled window is reliably honored by GNOME, so it stays above other
  windows.
- **Uninstall asks whether to keep the downloaded model.** On uninstall, if a
  model exists at `~/.local/share/whispertype/models`, the script prompts before
  removing it — keeping it means a reinstall skips the download.
- **Setup detects and reuses an existing model.** If
  `~/.local/share/whispertype/models` already has a downloaded model, setup
  reports it and leaves it intact instead of forcing a re-download.

## [3.8.0] - 2026-08-07

### Changed
- **Clean-slate model.** `./uninstall.sh` is now a silent, one-command nuke that
  removes everything this version of WhisperType installs — no prompts, and no
  handling or memory of any older version. Each install starts from scratch.
- The separate "remove legacy service" and "old model cache" steps are gone:
  uninstall simply removes the current install dir, helper service, desktop
  file, socket, and lock.

## [3.7.1] - 2026-08-07

### Fixed
- **uninstall.sh now reliably stops and removes the helper daemon.** The removal
  logic was rewritten to be robust against every install state: it detects the
  helper service by glob, stops/disables it, removes the unit file from any
  location, kills any stray daemon process (even one launched outside systemd),
  and deletes the install dir. Previously the detection only matched one exact
  name, so a mismatched service could be left running and installed.

## [3.7.0] - 2026-08-07

### Changed
- **Install under `~/.local/share/whispertype/`**: setup.sh now copies the app,
  creates its venv, and stores config all under this directory (instead of the
  git clone). Everything WhisperType owns is in one place: `app/`, `venv/`,
  `.env`, `models/`.
- **Model cache moved to `~/.local/share/whispertype/models/`** (was
  `~/.cache/whispertype/`), configurable via new `MODEL_DIR` env var. The model
  now lives inside the app install dir as requested.
- uninstall.sh updated for the new install location; also removes the old
  `~/.cache/whispertype/` location if present.
- README updated (config path, run path, install layout).

## [3.6.0] - 2026-08-07

### Added
- **Always-on-top floating record button** (frameless GTK window, native Wayland
  always-on-top — the same mechanism OSD/screenshot tools use, so it's robust).
  One click starts recording, one click stops and pastes. Draggable so it can be
  parked near where the user types. Hidden at startup.
- Tray left-click now toggles the floating button's visibility; the tray menu
  keeps "Start/Stop recording" and gains a "Show/Hide button" item. A close (×)
  on the floating button hides it back to the tray.

### Changed
- The floating button is the primary one-click control (the GNOME tray itself
  only opens a menu on click, so it can't do one-click recording reliably).
  The button color reflects state: green = ready, red = recording.
- README/About updated to describe the new flow.

## [3.5.0] - 2026-08-07

### Changed
- **Renamed back to WhisperType** (dropping "Key" since the hotkey is gone). All
  identifiers, files, the daemon/service/socket (`whispertype-helperd`,
  `/run/whispertype.sock`), and the Gitea repo were renamed.
- **Removed the global hotkey entirely** — it was bad UX (printed a stray "S" into
  the app you were typing in) and required a keyboard grab. The tray icon is now
  the sole trigger: click once to record, click again to stop and paste.
- **Helper daemon simplified to paste-only** (Rust): it no longer reads the
  keyboard or emits toggle events; it only injects Ctrl+V on a `paste` request.
  Smaller, less privileged surface.
- GUI: dropped the hotkey listener and hotkey config; added a "Start/Stop
  recording" tray-menu item so it works even on desktops where tray left-click
  activation isn't wired up.
- `whisper.py`: removed hotkey keycode resolution; `evdev` no longer a
  dependency. `.env.example` and README updated.

## [3.4.0] - 2026-08-07

### Added
- **Single-instance guard**: repeated clicks on the icon no longer spawn multiple
  tray icons. A file lock ensures only one WhisperType runs; a second launch shows
  a "already running" notification and exits.
- **Model-download progress window**: on first launch (model not cached), a modal
  dialog with a real progress bar + percent shows the download instead of the app
  silently hanging with no UI.
- **Left-click on the tray icon toggles recording** (in addition to the hotkey).
- **Custom colored tray icons** (green=idle, red=recording, amber=processing) via
  new `assets/icon-{idle,recording,processing}.svg`. Replaces the grey
  `microphone-sensitivity-muted-symbolic` GNOME icon, which was near-invisible on
  dark trays and misleadingly showed a "muted" slash.
- Model download is cached under `~/.cache/whispertype/models/<size>` and reused
  across launches.

### Changed
- `src/whisper.py`: added `download_model()`, `model_needs_download()`, and local
  model caching; `load_model()` prefers the local cache when present.

## [3.3.0] - 2026-08-07

### Changed
- **Helper daemon rewritten in Rust** (`src/helper/` is now a Cargo project).
  The old C version (`whispertype-helperd.c`) was removed. The Rust daemon:
  - is **memory-safe** by construction (Rust ownership/bounds guarantees)
  - uses **zero external crates** (pure `std` + hand-rolled libc FFI), so it
    builds **fully offline** with a minimal, auditable supply-chain surface
  - implements the same socket protocol, so the Python GUI is unchanged
- `install-helper.sh` now builds with `cargo build --release` (installs
  `rustc`/`cargo` if missing) instead of `gcc`.
- `README.md` updated: helper is now Rust, memory-safe, offline-buildable.

### Fixed
- The Rust `parse_config` correctly handles the space after `:` in the Python
  client's `json.dumps` output (a bug class the C version had). Unit tests cover
  `config`/`paste` detection and hotkey firing semantics (5 tests).

## [3.2.0] - 2026-08-07

### Changed
- **Renamed WhisperType → WhisperType** (in honor of the user's mother — the app
  reminds her to press the hotkey). All identifiers, filenames, service names,
  and the repo were updated (`whispertype-helperd`, `whispertype.sock`, etc.).

### Fixed
- **Desktop icon launches silently but nothing appears**: prereq failures were
  only `print()`ed then `sys.exit(1)`, which is invisible when launched from an
  icon (`Terminal=false`). Prereq/model-load/startup failures now show a GTK
  error dialog instead of silently exiting.
- **Icon click did nothing on GNOME**: `.desktop` file is now marked executable
  (GNOME requires this to trust/launch it), and `StartupNotify=false` is set.

## [3.1.0] - 2026-08-07

### Added
- **Privileged helper daemon (`whispertype-helperd`)**: a root-owned systemd
  service that owns all privileged input access — reading the physical keyboards
  for the global hotkey, and injecting Ctrl+V via `/dev/uinput`.
- **Clean architecture split**: the GUI (`app.py`) now runs as an unprivileged
  user with **no special groups** and talks to the daemon over a root-owned Unix
  socket (`/run/whispertype.sock`). The GUI sends `config`/`paste` requests and
  receives `toggle` events via a background listener thread.
- **No more `input` group / `uinput` udev rule**: the GUI no longer needs any
  special permissions. `setup.sh` builds and installs the helper via
  `install-helper.sh`.
- **Hardened socket auth**: the daemon uses `SO_PEERCRED` to accept connections
  only from non-root processes, and keeps `/dev/uinput` + the keyboards strictly
  inside the root daemon.
- `src/helper/whispertype-helperd.c` (C) and `src/helper_client.py` (Python
  socket client + hotkey listener thread).

### Changed
- `setup.sh` steps now: system packages → helper daemon → venv → config.
- `uninstall.sh` now removes the helper daemon (service + binary) instead of the
  udev rule / input group.
- README documents the two-process architecture and security model.

## [3.0.0] - 2026-08-07

### Added
- **Self-contained build**: the optional LM Studio / Ollama LLM-cleanup layer was
  removed entirely. WhisperType pulls the Whisper model on first run and transcribes
  fully offline — no external LLM, no cloud service, no GPU required.
- **Configurable global hotkey** via `.env` (`HOTKEY_MODS`, `HOTKEY_KEY`). Supports
  modifiers `super`/`ctrl`/`alt`/`shift` and keys `a-z` or names like `space`,
  `enter`, `tab`, `f1`–`f12`. All configured modifiers must be pressed (matches the
  original Super+Shift+S semantics).
- **AMD64 + ARM64 support**: CTranslate2 ships official aarch64 wheels, so the same
  code runs on both architectures.

### Changed
- Project renamed to **WhisperType**.
- `setup.sh` is now non-interactive (no theme prompt; dark icon used by default).
- Tray menu simplified to Status / About / Quit (LLM toggle and mode submenu removed).
- Hotkey shown dynamically in notifications and the About dialog.

### Removed
- `src/llm.py` (LM Studio client) and all LLM-related `.env` settings.

## [2.2.0] - 2026-06-09

### Added
- `DEVICE` env var to select computation device (`auto`, `cpu`, `cuda`)
- `COMPUTE_TYPE` env var to control precision (`int8`, `float16`, `float32`)
- Float16 guard: if `COMPUTE_TYPE=float16` is set but no CUDA GPU is detected,
  the app warns and automatically falls back to `int8`
- `uninstall.sh` — interactive script that walks through reversing every change
  made by `setup.sh` (desktop launcher, `.venv`, udev rule, group membership, packages)
- Theme-aware SVG launcher icon — `setup.sh` now asks whether you use a dark or
  light system theme and installs the matching icon variant

### Changed
- New branch `feature/openai-whisper-rocm` available as an alternative installation
  for AMD GPUs, CPU-only machines, or users who want broader hardware compatibility —
  it replaces faster-whisper with openai-whisper + PyTorch (supports NVIDIA CUDA,
  AMD ROCm, and CPU; `setup.sh` auto-detects and installs the correct PyTorch build)
- README: added Variants section comparing both branches with a separate `git clone`
  command for each so users can pick the right one for their hardware
- About dialog now shows the transcription backend (`faster-whisper + CTranslate2`)
- `setup.sh` banner now identifies which backend variant is being installed

### Fixed
- `setup.sh` now installs `build-essential` and `python3-dev` (required to compile
  some pip packages from source)
- Corrected run command in `setup.sh` output and `.env.example` (`app.py` → `src/app.py`)
- Removed unreachable `except TimeoutError` handler in `llm.py` — `urllib` wraps
  timeouts inside `URLError`, so that branch was never executed

## [2.1.0] - 2026-03-17

### Added
- Tray menu: LLM Formatting toggle (enable/disable at runtime)
- Tray menu: LLM Mode submenu to switch between `format` and `summarize`
- Tray menu: About dialog showing app name, version, and description
- All tray menu changes are persisted to `.env` and survive restarts

### Fixed
- `.env` path now resolved from `__file__` instead of CWD, ensuring tray menu changes
  are always saved regardless of how or from where the app is launched

## [2.0.1] - 2026-03-17

### Changed
- Moved source files into `src/` directory
- Entry point is now `src/app.py` (or via the app launcher)

## [2.0.0] - 2026-03-17

### Added
- Modular architecture: `app.py` (GTK tray + orchestration), `llm.py` (LM Studio client), `whisper.py` (audio + transcription)
- Optional LLM formatting via LM Studio (OpenAI-compatible API)
- `format` mode: remove filler words, fix false starts, correct punctuation
- `summarize` mode: condense transcription to key points
- `FORMATTING` state in the tray icon state machine
- `.env.example` configuration template — all settings documented with comments
- Per-event notification toggles (`NOTIFY_ON_READY`, `NOTIFY_ON_RECORDING`, etc.)
- `NOTIFY_VERBOSE` flag for detailed error messages
- `setup.sh` auto-creates `.env` from `.env.example` on first run
- LM Studio graceful fallback — dictation always works even if LLM is unavailable

### Changed
- All hardcoded constants moved to `.env` / environment variables
- `setup.sh` step count: 6 → 7 (added `.env` bootstrap step)

### Removed
- `dictate.py` — replaced by modular architecture above

## [1.0.0] - 2026-03-09

### Added
- Initial release: local speech-to-text dictation for Linux/Wayland
- `faster-whisper` transcription (offline, no cloud)
- `evdev` global hotkey listener (Super+Shift+S)
- GTK system tray icon via AyatanaAppIndicator3
- `wl-copy` + UInput Ctrl+V paste on Wayland
- Security hardening: max recording duration, sanitized logs, audio memory clearing