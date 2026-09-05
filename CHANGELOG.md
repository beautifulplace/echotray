# Changelog

## [2.3.2] - 2026-09-04

### Added
- **Offline unit test suite.** 38 tests covering model-cache completeness,
  download progress/cancellation, config parsing, model load/unload, and
  transcription. Runs with just the stdlib + pytest (no network, audio, or GPU).

### Fixed
- **Crash-proof config parsing.** A malformed `.env` value (e.g. a non-numeric
  `SAMPLE_RATE`) no longer crashes the app at import. A new `env_int` helper
  warns and falls back to the default, and clamps clearly-broken values.
- **Stale `.part` cleanup on cancel.** Cancelling a model download now removes
  the partial `.part` file instead of leaving it in the cache directory.

### Changed
- **Lazy heavy imports.** `numpy`, `sounddevice`, and `faster-whisper` now
  import inside the functions that use them, so importing the module stays
  stdlib-only and instant.

*Thanks to [@ghostfix-pm](https://github.com/ghostfix-pm) for the crash-proof
config parsing, lazy imports, `.part` cleanup, and the test suite.*

## [2.3.1] - 2026-09-04

### Changed
- **Setup window boxes match the About window.** The section boxes now use
  theme panel backgrounds (Gtk.ListBox) with no border, instead of the
  cairo-drawn rounded frame with a visible edge.

### Fixed
- **Model unload now returns memory to the OS.** Unloading a model freed
  CTranslate2's buffers but never called `_flush_memory()`, so glibc kept the
  freed pages in the process heap and RSS stayed at the high-water mark. Both
  unload paths (the load/unload toggle and deleting the loaded model) now flush
  memory so the freed RAM is actually returned to the OS.

## [2.3.0] - 2026-09-04

### Added
- **Modernized Setup window.** Two boxes with headings outside the box:
  "Whisper Model" and "Settings". The model box has a status light, a
  "Loaded: <model>" line with a load/unload toggle, a model dropdown (name
  left, download size right-justified, smallest to largest), Download and
  Delete buttons, and a full-width progress bar.
- **Model delete.** A Delete button removes a downloaded model from disk (with
  confirmation), so users can reclaim space after trying a large model.
- **Load/unload toggle.** Toggles the selected model in and out of memory
  without deleting it.
- **Download cancel.** The Delete button becomes a Cancel button while a
  download is in progress, aborting it cleanly.
- **Per-model file lists.** large-v3 uses `vocabulary.json` and
  `preprocessor_config.json` (not `vocabulary.txt`), fixing a 404 on download.

### Changed
- **Model switching during download.** Loading a different (already-downloaded)
  model now works while a download is in progress; the load and download paths
  run on separate threads.
- **Download auto-load.** A finished download only auto-loads the new model if
  no other model is loaded; otherwise it's cached for later.
- **Downloading/Cancel state is per-model.** The buttons only show
  "Downloading..."/"Cancel" for the model actually being downloaded; other
  models show their own Download/Delete state.

## [2.2.0] - 2026-09-03

### Added
- **X11 support.** The dictation paste path now picks the clipboard tool at
  runtime: `wl-copy` on Wayland, `xclip` on X11 (based on `WAYLAND_DISPLAY` vs
  `DISPLAY`). The uinput paste daemon is display-server agnostic, so this is the
  only change needed for X11. `install.sh` now also installs `xclip`.

## [2.1.1] - 2026-09-03

### Fixed
- **About window resets to the main page on close.** Closing the About window
  while on the Troubleshooting or Debugging Information page now resets the
  stack, so reopening it starts on the main page instead of the last-open
  subpage.

## [2.1.0] - 2026-09-03

### Added
- **Custom About window.** Replaced the native About dialog with a GNOME/Adwaita-style
  window: large centered icon, bold title, green version pill, the app description as
  a plain row, and Website + Troubleshooting rows. Troubleshooting slides over to a
  how-to page and then a debugging-information page (log viewer with Copy and Save As).
- **Headless smoke test** (`scripts/smoke_test.py` + `scripts/smoke-test.sh`) that
  builds the Setup and About windows under a virtual display to catch GTK API mistakes.

### Changed
- **Faster install.** `install.sh` now uses `uv` (parallel resolution, warm cache)
  instead of plain `pip`, matching the project's other repos.
- **Adwaita-style Setup window.** Header bar with a close button and bold section
  headers; control styling (rounded buttons/entries) that follows the system theme.
- **Fixed-size windows.** The About and Setup windows are non-resizable (minimize +
  close only, no maximize), and the About window stacks normally instead of floating
  above other windows.

## [2.0.14] - 2026-09-03

### Changed
- **Renamed install scripts.** `setup.sh` → `install.sh` (the single entry point,
  matching the `install.sh` convention used across the project's other repos) and
  `install-helper.sh` → `helper_install.sh` (so the privileged helper installer
  is clearly not the app installer).

## [2.0.13] - 2026-08-30

### Fixed
- **Memory no longer stays stuck at the high-water mark after dictation.** Each
  dictation now zeros and deletes the audio buffer, runs `gc.collect()`, and
  then calls `malloc_trim(0)` to return freed glibc heap pages back to the OS.
  The log now includes `[RSS] after flush ... kB` lines so the effect is
  visible.
- **Switching model sizes no longer briefly holds two Whisper models.** Before
  loading a new model, EchoTray unloads the previous CTranslate2 model, so
  swapping `small` -> `tiny` -> `small` can't spike RSS by holding both in
  memory.
- **RSS is now logged after model loads.** `[RSS] after model load ... kB` is
  printed whenever a model finishes loading, making it easy to see the
  steady-state footprint for the chosen size.

### Changed
- **Setup is now idempotent and faster.** `setup.sh` checks whether each
  required system package is already installed via `dpkg -s` and only runs
  `sudo apt install` for missing packages.
- **Helper daemon install is now idempotent.** `install-helper.sh` skips the
  build/install/service steps if the installed binary is current and the
  systemd service is already running.

## [2.0.12] - 2026-08-22

### Changed
- **Dynamic model button in the Setup window.** The button now reflects the
  selected model's state: "Download" when it isn't cached, "Load" when it's
  cached but not the currently loaded model, and "Ready" when it's the loaded
  model. Clicking "Load" loads the cached model without re-downloading.

## [2.0.11] - 2026-08-22

### Fixed
- **Tray icon stuck amber after a no-speech stop (real fix).** The 2.0.10
  toggle-through-disabled approach did not work: the grey and green sets both
  landed inside the GNOME AppIndicator extension's 30ms debounce window, so the
  transition was still collapsed. `set_idle()` now settles on green on a 150ms
  delay, so the amber -> green change lands outside the debounce window as a
  separate, genuine change the extension can't drop.

### Changed
- Added `CPU_THREADS=4` to `.env.example` (CTranslate2 thread cap).
- Added diagnostic logging (`[STATE]` / `[ICON]` lines) to state transitions and
  the icon poll timer.

## [2.0.10] - 2026-08-22

### Fixed
- **Tray icon still stuck amber after a no-speech stop (2.0.9 regression).**
  The 2.0.9 re-assert window was a no-op: libayatana-appindicator's
  `set_icon_full()` only emits the `NewIcon` DBus signal when the icon NAME
  actually changes, so re-asserting the same green icon every 500ms was
  silently dropped, and the GNOME extension's own equality check could also
  dedupe a green->green update against its stale cache. `set_idle()` now
  toggles through the disabled icon before settling on green, so the final
  green set is always a genuine name change the tray can't collapse away.

## [2.0.9] - 2026-08-20

### Fixed
- **Tray icon stuck amber after a no-speech stop.** A fast no-speech stop
  (red -> amber -> green in ~100ms) finished before the poll timer fired, so the
  icon guard saw "no change" and skipped the re-assert, leaving the tray stuck on
  amber. The state setters now update the tracked icon, and the timer re-asserts
  every tick during the transient states plus a short post-idle window, so a
  dropped icon self-heals without reintroducing the idle memory leak.
- **Memory leak while idle.** The 500ms icon poll timer called `set_icon_full()`
  every tick, which reloads the icon from disk on each call and leaks memory
  continuously. The timer now only calls it when the icon actually changes, so
  idle RSS stays flat.

### Changed
- **Reduced memory footprint via thread cap.** CTranslate2 (Whisper) now
  defaults to 4 CPU threads instead of one per core, which trims resident memory
  and often improves CPU latency by avoiding thread thrash. Configurable via
  `CPU_THREADS` in `.env`.

## [2.0.8] - 2026-08-19

### Fixed
- **Model size change now persists across restarts.** Choosing a new model size
  in Setup now writes `MODEL_SIZE` to `.env` and updates the in-memory size, so
  the app reloads the chosen size on next launch instead of reverting.
- **Uninstall no longer kills other dictation apps.** The broad
  `pkill -f "src/app.py"` pattern was removed; uninstall now matches only
  echotray paths.
- **Log file now flushes line-by-line.** stdout/stderr are reconfigured to
  line-buffered after the log redirect, so diagnostics actually reach
  `/tmp/echotray.log` instead of sitting in Python's block buffer.
- **Recorder stop can no longer freeze the app.** `AudioRecorder.stop()` runs
  the stream teardown in a background thread with a 3s timeout, so a wedged
  audio device degrades gracefully instead of hanging the tray.
- **Model completeness now checks all files.** The download/load checks verify
  every required file (config.json, tokenizer.json, vocabulary.txt, model.bin)
  is present and non-empty, not just `model.bin`.
- **Download has a timeout.** A 30s socket timeout prevents a stalled
  connection from hanging the download forever.
- **Progress bar advances smoothly.** Download progress is reported across all
  files instead of resetting to 0% for each file.
- **Helper daemon no longer blocks on a stalled client.** Accepted sockets are
  non-blocking with per-client buffers, so a client that connects but never
  sends a newline can't freeze the daemon.

### Changed
- **About dialog is cached** and reused across clicks.
- **Quit stops an active recorder** cleanly before exiting.
- **setup.sh is idempotent** - it removes any prior copy before re-copying, so
  a re-run no longer nests into `src/src/`.
- **Removed stale "hotkey" wording** from the daemon's Cargo.toml description
  and systemd unit (the daemon is paste-only).

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

## [1.0.0] - 2026-03-09

### Added
- Initial release: local speech-to-text dictation for Linux/Wayland
- `faster-whisper` transcription (offline, no cloud)
- `evdev` global hotkey listener (Super+Shift+S)
- GTK system tray icon via AyatanaAppIndicator3
- `wl-copy` + UInput Ctrl+V paste on Wayland
- Security hardening: max recording duration, sanitized logs, audio memory clearing
