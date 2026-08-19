# Changelog

## [2.0.7] - 2026-08-19

### Added
- **Setup window for the Whisper model.** A new non-modal Setup window (opened
  from a new **Setup...** menu item) shows a status light (green when a model
  is loaded, red when none), the currently loaded model, a dropdown to change
  model size (tiny/base/small/medium/large-v3), a Download button, and a live
  progress bar. The download is owned by the app and continues even if the
  window is closed. This is now the way to install the model on first run and
  to change the model size later.
- **Three more settings in the Setup window**, each in its own framed section
  below the Whisper Model section (no status lights):
  - **Language** - a dropdown to set the dictation language (Auto-detect,
    English, French, German, Spanish, Italian, Portuguese, Dutch). Writes
    `WHISPER_LANGUAGE` and applies live.
  - **Paste delay** - a spin box for the delay between copying the text and
    pasting it (ms). Increase if the paste comes out blank. Writes
    `PASTE_DELAY_MS` and applies live.
  - **Max recording length** - a spin box for how long a recording can run
    before auto-stopping (seconds). Writes `MAX_RECORDING_SECONDS` and applies
    live.
- Settings are persisted to `.env` and take effect immediately (no restart).

### Changed
- **Menu now matches EchoTalk's shape.** The tray menu is now: Status, Start
  recording, Setup..., About, Quit. The "Check status" item was removed, and
  "Install model…" was replaced by the **Setup...** window.

### Fixed
- **Tray icon was green at startup with no model loaded.** The 500ms icon
  re-assert poll timer showed the green idle icon whenever the state was IDLE,
  even before a model was loaded, overriding the grey disabled icon. It now
  keeps the grey "Waiting for model" icon until a model is actually loaded.
- **Setup window no longer grows to the longest sentence.** The window width is
  now capped (default 420px, min 360px) and the hint labels wrap at a fixed
  character width, so long hint text no longer stretches the window. Height
  still auto-sizes to fit the sections.
- **Notifications no longer stack up in the notification tray.** All
  `notify-send` messages are now marked transient (`transient:true`) with a 5s
  timeout, so the notification daemon shows them briefly and dismisses them
  instead of keeping every one in the tray/center. This stops the tray from
  filling to the top of the screen and hiding notifications you missed.

## [2.0.6] - 2026-08-13

### Fixed
- **Detect any already-downloaded model.** EchoTray now auto-loads ANY cached
  model (not just the configured size) via `whisper.downloaded_model_sizes()`,
  so a downloaded model is detected and the tray goes ready without clicking
  "Install model…".
- **Tray icon could revert while recording.** A 500ms poll timer now re-asserts
  the tray icon from the current state, self-healing the glitch where the icon
  reverted to idle-green while still recording.

## [2.0.5] - 2026-08-13

### Added
- **Tray-first startup.** EchoTray now builds the tray icon immediately (grey,
  "Start Recording" greyed out) instead of exiting when prerequisites are
  missing. The app never hard-exits on a missing model or missing environment
  checks - the tray is always up.
- **Menu-driven model install.** A new "Install model…" menu item downloads and
  loads the Whisper model, then flips the app to ready (green, Start Recording
  enabled). Hidden once the model is installed.
- **"Check status" menu item.** Re-runs the environment checks (helper daemon,
  wl-copy, audio) and reports the result as a notification, so dictation
  problems can be diagnosed without the app quitting.

### Changed
- **Cached model auto-loads at startup.** If the Whisper model is already
  downloaded, EchoTray loads it in the background on launch and goes straight
  to ready - no need to click "Install model…". Only when the model is NOT
  cached does the tray stay grey and wait for the user to install it.

## [2.0.4] - 2026-08-10

### Changed
- **Done notification now reports word count + total time.** The "Transcribed"
  popup shows `N words in X.Xs` (recording + transcription time) instead of the
  previous character count / transcription-only time.
- **Removed the "Stopped - Transcribing..." popup on stop.** It occupied the
  notification slot and made the "Transcribed" popup fire late on GNOME. The
  tray icon already turns amber as the visual cue, so the completion popup now
  appears promptly. The `NOTIFY_ON_TRANSCRIBING` flag was removed (it is no
  longer used).

## [2.0.3] - 2026-08-10

### Changed
- **Renamed the project to EchoTray.** Earlier public names (WhisperType, then
  WhisperTray) collided with existing dictation apps on other platforms, so the
  app is now **EchoTray** (fitting its tray-only design). This renames the app,
  the helper daemon (`echotray-helperd`), the Unix socket
  (`/run/echotray.sock`), the install directories, and the desktop launcher.
- **Removed the NVIDIA GPU option.** EchoTray now always runs on CPU (int8).
  The `DEVICE` setting and GPU auto-detection were removed - it works
  identically on any machine, including ones with an NVIDIA GPU where
  CUDA/float16 caused problems. `float16` is no longer supported (it is forced
  to int8 if set).

### Added
- **Model size choice on first launch.** EchoTray now lets you pick between
  `small` (best accuracy, default), `base` (faster), and `tiny` (fastest for
  low-end hardware) instead of always downloading `small`. Changeable later via
  `MODEL_SIZE` in `.env`.
- **CLI wrapper.** `setup.sh` now installs an `echotray` command (a wrapper at
  `~/.local/share/echotray/echotray` symlinked into `~/.local/bin`), so the app
  runs from the terminal with just `echotray`. Removed on uninstall.

### Changed (interaction / UI)
- **Tray-only app.** Removed the floating window (GNOME focus rules made it
  unreliable). Left-click the tray icon toggles recording; the tray menu has
  Start/Stop recording. Tray icons are now bold round green/red/amber buttons.
- **Trailing space after each dictation (always on).** Every transcription ends
  with a space so consecutive dictations don't run together. Whisper's natural
  punctuation is kept (a period is not forced).
- **About dialog no longer freezes the app** and shows the app icon; the logo is
  scaled to 128px so the About text is readable on small screens.

### Fixed
- **Reduced memory growth over time** (per-utterance objects are garbage-
  collected after each transcription).
- **Fixed a freeze when dictating two sentences in a row** (transcription is now
  serialized with a lock; CTranslate2 is not thread-safe on a shared model).
- **Clicking the record button no longer steals keyboard focus**, so the
  injected Ctrl+V lands at the cursor in the document, not in the app.
- **Setup no longer looks stuck** while installing the Rust toolchain (apt
  output is shown, not suppressed with `-qq`).
- **Install `libnotify-bin`** so desktop notifications work out of the box on
  Debian.
- **Silenced pip uninstall warnings during setup** (`--ignore-installed`).

## [2.2.0] - 2026-06-09

### Added
- `DEVICE` env var to select computation device (`auto`, `cpu`, `cuda`)
- `COMPUTE_TYPE` env var to control precision (`int8`, `float16`, `float32`)
- Float16 guard: if `COMPUTE_TYPE=float16` is set but no CUDA GPU is detected,
  the app warns and automatically falls back to `int8`
- `uninstall.sh` - interactive script that walks through reversing every change
  made by `setup.sh` (desktop launcher, `.venv`, udev rule, group membership, packages)
- Theme-aware SVG launcher icon - `setup.sh` now asks whether you use a dark or
  light system theme and installs the matching icon variant

### Changed
- New branch `feature/openai-whisper-rocm` available as an alternative installation
  for AMD GPUs, CPU-only machines, or users who want broader hardware compatibility -
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
- Removed unreachable `except TimeoutError` handler in `llm.py` - `urllib` wraps
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
- `.env.example` configuration template - all settings documented with comments
- Per-event notification toggles (`NOTIFY_ON_READY`, `NOTIFY_ON_RECORDING`, etc.)
- `NOTIFY_VERBOSE` flag for detailed error messages
- `setup.sh` auto-creates `.env` from `.env.example` on first run
- LM Studio graceful fallback - dictation always works even if LLM is unavailable

### Changed
- All hardcoded constants moved to `.env` / environment variables
- `setup.sh` step count: 6 → 7 (added `.env` bootstrap step)

### Removed
- `dictate.py` - replaced by modular architecture above

## [1.0.0] - 2026-03-09

### Added
- Initial release: local speech-to-text dictation for Linux/Wayland
- `faster-whisper` transcription (offline, no cloud)
- `evdev` global hotkey listener (Super+Shift+S)
- GTK system tray icon via AyatanaAppIndicator3
- `wl-copy` + UInput Ctrl+V paste on Wayland
- Security hardening: max recording duration, sanitized logs, audio memory clearing
