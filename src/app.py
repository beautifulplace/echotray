#!/usr/bin/env python3
"""EchoTray - click the tray microphone to dictate text at your cursor.

Self-contained: pulls the Whisper model on first run and transcribes entirely
offline. No external LLM, no GPU required (runs on CPU via int8).

Click the tray icon once to start recording, click again to stop; the transcript
is pasted at your cursor. No hotkey, no keyboard grab - the tray icon is the
trigger.

Privileged input (Ctrl+V injection) is handled by the root-owned
echotray-helperd daemon. The GUI runs as an unprivileged user with no special
groups and talks to the daemon over /run/echotray.sock.
"""

__version__ = "2.0.5"

import os
import pathlib
import gc
import shutil
import signal
import subprocess
import sys
import threading
import time

import gi
import sounddevice as sd
from dotenv import load_dotenv

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import GLib, Gtk  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as appindicator  # noqa: E402

import helper_client
import whisper

load_dotenv()

_DOTENV_PATH = pathlib.Path(__file__).resolve().parent.parent / ".env"

# Startup log: everything printed (and any traceback) is also written here, so
# launching from a desktop icon (no terminal) still leaves a record of what
# happened. Check it whenever the app "does nothing".
_LOG_PATH = pathlib.Path(os.getenv("WHISPER_LOG", "/tmp/echotray.log"))

# ── Configuration (from .env) ─────────────────────────────────────────────────

PASTE_DELAY_MS = int(os.getenv("PASTE_DELAY_MS", "100"))

# Custom colored tray icons (SVG in assets/) - far more visible than the grey
# GNOME symbolic icons, and color-coded by state:
#   idle       = green  (ready to dictate)
#   recording  = red    (mic live)
#   processing = amber  (transcribing)
# We pass absolute file paths to set_icon_full (the Python GIR binding does not
# expose set_icon_theme_path, so icon-theme lookup by name would fail).
_ASSETS_DIR = pathlib.Path(__file__).resolve().parent.parent / "assets"
ICON_IDLE = str(_ASSETS_DIR / "icon-idle.svg")
ICON_RECORDING = str(_ASSETS_DIR / "icon-recording.svg")
ICON_PROCESSING = str(_ASSETS_DIR / "icon-processing.svg")
ICON_DISABLED = str(_ASSETS_DIR / "icon-disabled.svg")

# Notification toggles - each can be disabled independently via .env
NOTIFY_ON_READY        = os.getenv("NOTIFY_ON_READY",        "true").lower() == "true"
NOTIFY_ON_RECORDING    = os.getenv("NOTIFY_ON_RECORDING",    "true").lower() == "true"
NOTIFY_ON_DONE         = os.getenv("NOTIFY_ON_DONE",         "true").lower() == "true"
NOTIFY_ON_SKIPPED      = os.getenv("NOTIFY_ON_SKIPPED",      "true").lower() == "true"
NOTIFY_VERBOSE         = os.getenv("NOTIFY_VERBOSE",         "false").lower() == "true"


# ── Notifications ─────────────────────────────────────────────────────────────

def notify(summary, body="", icon="audio-input-microphone", urgency="normal"):
    """Send a desktop notification and print to terminal."""
    print(f"[{summary}] {body}" if body else f"[{summary}]")
    args = ["notify-send", "-i", icon, "-u", urgency, summary]
    if body:
        args.append(body)
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except FileNotFoundError:
        pass


def show_error_dialog(message):
    """Show a blocking GTK error dialog. Used when launching from a desktop icon
    (no terminal), so failures are visible instead of silently exiting."""
    dialog = Gtk.MessageDialog(
        transient_for=None,
        flags=0,
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.OK,
        text="EchoTray failed to start",
    )
    dialog.format_secondary_text(message)
    dialog.run()
    dialog.destroy()


# ── Single-instance guard ─────────────────────────────────────────────────────

_LOCK_PATH = "/tmp/echotray.lock"


def _acquire_single_instance() -> bool:
    """Ensure only one EchoTray instance runs. Returns True if we got the lock."""
    try:
        fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Write our PID so diagnostics can see who holds the lock.
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return True
    except (BlockingIOError, OSError):
        # Another instance holds the lock.
        return False


# ── Model download progress window ────────────────────────────────────────────

def show_download_progress_dialog(model_size):
    """Return a (window, progress_bar, status_label) and a callback to update them.

    Runs the download in a background thread and updates a modal dialog with a
    progress bar, so first-launch (model download) is visible instead of the app
    silently hanging with no UI.
    """
    dialog = Gtk.Dialog(
        title="EchoTray - downloading model",
        transient_for=None,
        flags=0,
    )
    dialog.set_default_size(420, -1)
    # Ensure the dialog is impossible to miss: keep it above other windows and
    # bring it to the front when it appears (otherwise on first-launch the
    # download can run for minutes with no visible UI).
    dialog.set_keep_above(True)
    dialog.set_modal(True)

    box = dialog.get_content_area()
    label = Gtk.Label(
        label=(
            f"Downloading the Whisper '{model_size}' model (~{_model_size_mb(model_size)} MB).\n"
            "This happens once and is cached locally.\n"
            "Please wait - dictation will be ready when this finishes."
        )
    )
    label.set_line_wrap(True)
    box.pack_start(label, False, False, 6)

    progress = Gtk.ProgressBar()
    progress.set_show_text(True)
    progress.set_text("0%")
    box.pack_start(progress, False, False, 6)

    status = Gtk.Label(label="Connecting…")
    box.pack_start(status, False, False, 4)

    dialog.show_all()
    dialog.present()  # bring to front so the user sees the download is happening

    def update(fraction, _bytes):
        pct = int(fraction * 100)
        progress.set_fraction(fraction)
        progress.set_text(f"{pct}%")
        if pct >= 100:
            status.set_text("Download complete - loading model…")
        else:
            status.set_text(f"Downloaded {pct}%")

    return dialog, update


def _model_size_mb(size):
    # Approximate model.bin sizes for the progress text.
    return {"tiny": 75, "base": 145, "small": 466, "medium": 1519, "large-v3": 3018}.get(size, 466)


def show_model_choice_dialog():
    """Let the user pick a Whisper model size before the first download.

    Defaults to 'small' (best accuracy), but offers smaller/faster models for
    older or low-memory systems. Returns the chosen size string, or None if the
    user cancels.
    """
    dialog = Gtk.Dialog(
        title="EchoTray - choose model",
        transient_for=None,
        flags=0,
    )
    dialog.set_default_size(440, -1)
    dialog.set_keep_above(True)
    dialog.set_modal(True)

    box = dialog.get_content_area()

    intro = Gtk.Label(
        label=(
            "EchoTray needs to download a Whisper model (once, cached locally).\n"
            "Choose the size that fits your system:"
        )
    )
    intro.set_line_wrap(True)
    box.pack_start(intro, False, False, 6)

    # Radio buttons: small (default) / base / tiny
    radio_small = Gtk.RadioButton.new_with_label_from_widget(None, "small - best accuracy (recommended, ~466 MB)")
    radio_small.set_active(True)
    box.pack_start(radio_small, False, False, 4)

    radio_base = Gtk.RadioButton.new_with_label_from_widget(radio_small, "base - faster, good for older systems (~145 MB)")
    box.pack_start(radio_base, False, False, 4)

    radio_tiny = Gtk.RadioButton.new_with_label_from_widget(radio_small, "tiny - fastest, for very low-end hardware (~75 MB)")
    box.pack_start(radio_tiny, False, False, 4)

    hint = Gtk.Label(label="Tip: you can change this later in ~/.local/share/echotray/.env (MODEL_SIZE).")
    hint.set_line_wrap(True)
    box.pack_start(hint, False, False, 6)

    dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button("Download", Gtk.ResponseType.OK)
    dialog.show_all()
    dialog.present()

    response = dialog.run()
    chosen = None
    if response == Gtk.ResponseType.OK:
        if radio_base.get_active():
            chosen = "base"
        elif radio_tiny.get_active():
            chosen = "tiny"
        else:
            chosen = "small"
    dialog.destroy()
    return chosen


# ── Prerequisite Checks ───────────────────────────────────────────────────────

def check_prerequisites():
    """Return a list of error strings (empty = all good). Does not exit."""
    errors = []

    # Check wl-copy
    if not shutil.which("wl-copy"):
        errors.append(
            "'wl-copy' not found. Install it with:\n"
            "    sudo apt install wl-clipboard"
        )

    # Check the helper daemon is running (paste-only service; no config needed)
    try:
        client = helper_client.HelperClient()
        client.send_paste_probe()  # probe the socket (no-op paste is not sent; just connect)
    except helper_client.HelperError as e:
        errors.append(
            "The EchoTray helper daemon is not running:\n"
            f"    {e}\n\n"
            "Start it with:\n"
            "    sudo systemctl start echotray-helperd\n"
            "or re-run setup.sh."
        )

    # Check audio input
    try:
        sd.query_devices(kind="input")
    except sd.PortAudioError:
        errors.append(
            "No audio input device found.\n"
            "  Check that your microphone is connected and PipeWire is running."
        )

    return errors


# ── Text Pasting ──────────────────────────────────────────────────────────────

def paste_text(text):
    """Copy text to clipboard via wl-copy, then ask the helper to inject Ctrl+V."""
    try:
        # Step 1: Copy to Wayland clipboard
        subprocess.run(["wl-copy", "--", text], check=True, timeout=5)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[ERROR] wl-copy: {e}")
        body = f"wl-copy failed: {e}" if NOTIFY_VERBOSE else "Clipboard copy failed - check terminal for details."
        notify("Error", body, urgency="critical")
        return

    # Step 2: Small delay so clipboard is ready
    time.sleep(PASTE_DELAY_MS / 1000)

    # Step 3: Ask the privileged helper to simulate Ctrl+V
    try:
        helper_client.HelperClient().send_paste()
    except helper_client.HelperError as e:
        print(f"[ERROR] paste request: {e}")
        body = f"Paste failed: {e}\nText copied to clipboard - paste manually." if NOTIFY_VERBOSE else "Paste failed - text is on clipboard, paste manually."
        notify("Error", body, urgency="critical")


# ── Transcription Orchestration ───────────────────────────────────────────────

def transcribe_and_paste(model, audio_data, app):
    # Reject recordings that are too short to contain speech
    if len(audio_data) < whisper.SAMPLE_RATE // 10:
        if NOTIFY_ON_SKIPPED:
            notify("Skipped", "Recording too short")
        GLib.idle_add(app.set_idle)
        return

    start = time.monotonic()
    try:
        text = whisper.transcribe_audio(model, audio_data)
    except Exception as e:
        print(f"[ERROR] Transcription: {e}")
        body = f"Transcription failed: {e}" if NOTIFY_VERBOSE else "Transcription failed - check terminal for details."
        notify("Error", body, urgency="critical")
        GLib.idle_add(app.set_idle)
        return

    elapsed = time.monotonic() - start

    if not text:
        if NOTIFY_ON_SKIPPED:
            notify("Skipped", f"No speech detected ({elapsed:.1f}s)")
        GLib.idle_add(app.set_idle)
        return

    # Report total time (recording + transcription) and word count, not just the
    # transcription step and character count. Record start is the monotonic time
    # the user clicked to start; if it's unavailable (e.g. auto-stop edge), fall
    # back to the transcription duration.
    total_time = elapsed
    if getattr(app, "_record_start", None) is not None:
        total_time = time.monotonic() - app._record_start
        app._record_start = None
    word_count = len(text.split())

    print(f"[TRANSCRIBED] ({total_time:.1f}s, {word_count} words)")

    paste_text(text)
    if NOTIFY_ON_DONE:
        notify("Transcribed", f"{word_count} words in {total_time:.1f}s")
    audio_data[:] = 0
    del audio_data
    # Force Python to reclaim per-utterance objects (audio buffer, segment
    # generators, VAD state) now, rather than letting them accumulate across
    # many dictations. The Whisper model itself (~1.3GB for 'small') stays
    # loaded - that's the fixed baseline - but transient memory is freed here.
    gc.collect()
    GLib.idle_add(app.set_idle)


# ── Tray Icon App ─────────────────────────────────────────────────────────────

class DictationApp:
    def __init__(self, model=None):
        self.model = model
        self.state = "IDLE"
        self.recorder = None
        self._recording_timeout_id = None
        self._record_start = None  # monotonic time recording began (for total-time report)
        self._check_item = None

        # Set up tray indicator using our custom colored icons (absolute paths)
        self.indicator = appindicator.Indicator.new(
            "echotray",
            ICON_DISABLED if model is None else ICON_IDLE,
            appindicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(appindicator.IndicatorStatus.ACTIVE)

        # Left-click on the tray toggles recording (one-click dictation).
        try:
            self.indicator.connect("activate", self._on_indicator_click)
        except Exception:
            pass

        # Build menu
        menu = Gtk.Menu()

        self.status_item = Gtk.MenuItem(label="Status: Idle")
        self.status_item.set_sensitive(False)
        menu.append(self.status_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Toggle recording from the menu too. Greyed out until the model is ready.
        self.toggle_item = Gtk.MenuItem(label="Start recording")
        if model is None:
            self.toggle_item.set_sensitive(False)
        self.toggle_item.connect("activate", self._on_toggle_menu)
        menu.append(self.toggle_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Check status - re-runs the environment checks (helper daemon, wl-copy,
        # audio) without exiting. Useful when dictation doesn't work: the tray
        # stays up and this tells you what's wrong.
        check_item = Gtk.MenuItem(label="Check status")
        check_item.connect("activate", self._on_check_status)
        menu.append(check_item)
        self._check_item = check_item

        menu.append(Gtk.SeparatorMenuItem())

        # Install/download the Whisper model (only needed on first run, or after
        # the model dir was cleared). Hidden once the model is ready.
        install_item = Gtk.MenuItem(label="Install model…")
        install_item.connect("activate", self._install_model)
        menu.append(install_item)
        self._install_item = install_item

        menu.append(Gtk.SeparatorMenuItem())

        about_item = Gtk.MenuItem(label="About")
        about_item.connect("activate", self._on_about)
        menu.append(about_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self._on_quit)
        menu.append(quit_item)

        menu.show_all()
        self.indicator.set_menu(menu)

    def _on_toggle_menu(self, _widget):
        """Tray-menu 'Start/Stop recording' toggles recording."""
        self._toggle()

    def _on_indicator_click(self, *_args):
        """Left-click on the tray icon toggles recording (one-click dictation)."""
        self._toggle()

    def _on_check_status(self, _widget):
        """Re-run the environment checks and report the result as a notification."""
        errors = check_prerequisites()
        if errors:
            notify("Status", "Problems found:\n" + "\n".join(errors), urgency="critical")
        else:
            notify("Status", "All checks passed.")

    def _install_model(self, _widget):
        """Download (if needed) and load the Whisper model, then enable dictation.

        Runs on the GTK main thread (it is a menu callback), so all dialog work
        happens here. The slow network download and model load run in background
        threads. On success flips the app to ready; on failure/cancel the tray
        stays up (disabled) and the user can retry.
        """
        if self.model is not None:
            return  # already installed
        model_size = whisper.MODEL_SIZE

        if whisper.model_needs_download(model_size):
            chosen = show_model_choice_dialog()
            if chosen is None:
                print("Model selection cancelled by user.")
                return
            model_size = chosen
            print(f"Downloading model '{model_size}' with progress UI...")
            dialog, progress_cb = show_download_progress_dialog(model_size)

            result = {"ok": False, "error": None}
            dialog_done = {"closed": False}

            def _download_job():
                try:
                    # progress_cb touches GTK widgets -> must run on the main thread.
                    def cb(fraction, bytes_done):
                        GLib.idle_add(progress_cb, fraction, bytes_done)
                    whisper.download_model(model_size, progress_cb=cb)
                    result["ok"] = True
                except Exception as e:  # noqa: BLE001
                    result["error"] = e
                def _close():
                    if not dialog_done["closed"]:
                        dialog_done["closed"] = True
                        dialog.destroy()
                GLib.idle_add(_close)

            threading.Thread(target=_download_job, daemon=True).start()

            # Block the main loop on the progress dialog; it auto-closes when the
            # download thread finishes. The download itself runs off-thread.
            dialog.run()
            dialog_done["closed"] = True
            dialog.destroy()

            if not result["ok"]:
                if result["error"] is not None:
                    print(f"[ERROR] Model download failed: {result['error']}")
                    notify("EchoTray", f"Model download failed: {result['error']}", urgency="critical")
                else:
                    print("Download cancelled by user.")
                return

        # Slow model load runs off the main thread; enable the app when done.
        app = self
        def _load_job():
            try:
                model = whisper.load_model(model_size)
            except Exception as e:
                print(f"[ERROR] Model load failed: {e}")
                GLib.idle_add(notify, "EchoTray", f"Model load failed: {e}", "audio-input-microphone", "critical")
                return
            GLib.idle_add(app._activate_model, model)

        threading.Thread(target=_load_job, daemon=True).start()

    def _activate_model(self, model):
        """Set the loaded model on the app and flip it to ready (main thread)."""
        self.model = model
        self.set_ready()
        if NOTIFY_ON_READY:
            notify("Ready", "Click the round tray microphone to dictate")

    def set_disabled(self):
        """Pre-model state: grey icon, Start Recording greyed out."""
        self.state = "IDLE"
        self.indicator.set_icon_full(ICON_DISABLED, "Waiting for model")
        self.status_item.set_label("Status: waiting for model")
        try:
            self.toggle_item.set_sensitive(False)
        except Exception:
            pass

    def set_ready(self):
        """Post-model state: green idle icon, Start Recording enabled."""
        self.state = "IDLE"
        self.indicator.set_icon_full(ICON_IDLE, "Idle")
        self.status_item.set_label("Status: Idle")
        try:
            self.toggle_item.set_sensitive(True)
            self.toggle_item.set_label("Start recording")
        except Exception:
            pass
        try:
            # The model is installed now; hide the Install item.
            self._install_item.hide()
        except Exception:
            pass

    def set_idle(self):
        self.state = "IDLE"
        self.indicator.set_icon_full(ICON_IDLE, "Idle")
        self.status_item.set_label("Status: Idle")
        try:
            self.toggle_item.set_label("Start recording")
        except Exception:
            pass

    def set_recording(self):
        self.state = "RECORDING"
        self.indicator.set_icon_full(ICON_RECORDING, "Recording")
        self.status_item.set_label("Status: Recording...")
        try:
            self.toggle_item.set_label("Stop recording")
        except Exception:
            pass

    def set_transcribing(self):
        self.state = "TRANSCRIBING"
        self.indicator.set_icon_full(ICON_PROCESSING, "Transcribing")
        self.status_item.set_label("Status: Transcribing...")

    def _on_about(self, _widget):
        dialog = Gtk.AboutDialog()
        dialog.set_program_name("EchoTray")
        dialog.set_version(__version__)
        # Use the same icon as the tray (icon-idle.svg) so the About dialog and
        # the tray/desktop launcher match. Scale it down - the source SVG is
        # 64px and would render large in the dialog.
        _LOGO = str(pathlib.Path(__file__).resolve().parent.parent / "assets" / "icon-idle.svg")
        if os.path.isfile(_LOGO):
            try:
                from gi.repository import GdkPixbuf
                _pb = GdkPixbuf.Pixbuf.new_from_file(_LOGO)
                _pb = _pb.scale_simple(128, 128, GdkPixbuf.InterpType.BILINEAR)
                dialog.set_logo_icon_name("")
                dialog.set_logo(_pb)
            except Exception:
                pass
        dialog.set_comments(
            "Speech-to-text dictation, fully offline and self-contained.\n"
            "Click the round tray microphone to start/stop recording; the\n"
            "transcript is pasted at your cursor.\n\n"
            "Transcription: faster-whisper + CTranslate2 (CPU, no GPU required)\n"
            "Model is downloaded on first run and cached locally.\n\n"
            "Paste (Ctrl+V) handled by the root-owned echotray-helperd,\n"
            "so EchoTray runs unprivileged with no special groups."
        )
        # Show the dialog NON-modally (show + present, not run()). dialog.run()
        # blocks the main loop, which would freeze the tray icon - so recording
        # couldn't be stopped and the app couldn't be quit while About was open.
        dialog.set_modal(False)
        dialog.connect("response", lambda d, _r: d.destroy())
        dialog.show()
        dialog.present()
        # The About dialog's comments text view grabs keyboard focus and shows a
        # blinking text cursor even though nothing is editable. Clear focus when
        # it appears so no cursor is shown.
        dialog.connect("show", lambda d: d.set_focus(None))

    def _on_quit(self, _widget):
        self.loop.quit()

    def _auto_stop(self):
        if self.state == "RECORDING":
            notify("Recording", "Auto-stopped (max duration reached)")
            self._toggle()
        return False  # don't repeat

    def _toggle(self):
        if self.model is None:
            # The model isn't loaded yet - don't attempt to record.
            notify("EchoTray", "Still loading the model - try again shortly.")
            return
        if self.state == "IDLE":
            try:
                self.recorder = whisper.AudioRecorder()
                self.recorder.start()
                self._record_start = time.monotonic()
                self.set_recording()
                self._recording_timeout_id = GLib.timeout_add_seconds(
                    whisper.MAX_RECORDING_SECONDS, self._auto_stop
                )
                if NOTIFY_ON_RECORDING:
                    notify("Recording", "Speak now...")
            except Exception as e:
                print(f"[ERROR] Recording: {e}")
                self.recorder = None
                self.set_idle()
                body = f"Could not start recording: {e}" if NOTIFY_VERBOSE else "Could not start recording - check terminal for details."
                notify("Error", body, urgency="critical")
        elif self.state == "RECORDING":
            if self._recording_timeout_id is not None:
                GLib.source_remove(self._recording_timeout_id)
                self._recording_timeout_id = None
            self.set_transcribing()
            # No notification on stop: the tray icon already turns amber as the
            # visual cue, and popping a "Transcribing..." notification here would
            # occupy the notification slot and delay the important "Transcribed"
            # (word count + time) popup that follows.
            # Stop the recorder; guard against it being None so a stop can never
            # get stuck in the TRANSCRIBING state.
            recorder = self.recorder
            self.recorder = None
            try:
                audio_data = recorder.stop() if recorder is not None else None
            except Exception as e:
                print(f"[ERROR] Stopping recorder: {e}")
                audio_data = None
            self.state = "TRANSCRIBING"
            if audio_data is not None:
                threading.Thread(
                    target=transcribe_and_paste,
                    args=(self.model, audio_data, self),
                    daemon=True,
                ).start()
            else:
                # Nothing to transcribe - recover to idle so the next click works.
                self.set_idle()

    def run(self):
        self.loop = GLib.MainLoop()
        # The "Ready" notification now fires from _load_and_enable() once the
        # model is actually loaded (not here), so the tray can start disabled.
        print("\nEchoTray running (tray icon visible).\n")
        self.loop.run()


# ── Main ──────────────────────────────────────────────────────────────────────

def _enable_log_file():
    """Redirect stdout/stderr to also be written to the log file, so a desktop-icon
    launch (no terminal) leaves a record of what the app did."""
    try:
        log_fd = os.open(_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(log_fd, 1)  # stdout
        os.dup2(log_fd, 2)  # stderr
        if log_fd > 2:
            os.close(log_fd)
    except OSError:
        pass


def main():
    _enable_log_file()
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

    # Only one instance may run; repeated clicks must not spawn more tray icons.
    if not _acquire_single_instance():
        print("EchoTray is already running (single instance).")
        # Bring attention via notification; don't start a second copy.
        try:
            subprocess.Popen(
                ["notify-send", "-i", "audio-input-microphone", "EchoTray",
                 "EchoTray is already running - check the tray."],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass
        sys.exit(0)

    print("EchoTray")
    print("==========")
    print()

    # Build the tray immediately (grey, disabled). The app never exits on a
    # missing model or missing environment checks - the tray is always up.
    # If the model is already cached, load it in the background and flip to
    # ready. Only if it's NOT cached do we stay disabled and wait for the user
    # to click "Install model…".
    app = DictationApp(model=None)
    app.set_disabled()

    if not whisper.model_needs_download(whisper.MODEL_SIZE):
        print(f"Model '{whisper.MODEL_SIZE}' is cached - loading in the background...")
        def _load():
            try:
                model = whisper.load_model(whisper.MODEL_SIZE)
            except Exception as e:
                print(f"[ERROR] Model load failed: {e}")
                GLib.idle_add(notify, "EchoTray", f"Model load failed: {e}", "audio-input-microphone", "critical")
                return
            GLib.idle_add(app._activate_model, model)
        threading.Thread(target=_load, daemon=True).start()

    try:
        app.run()
    except SystemExit:
        pass
    except Exception as e:
        print(f"[ERROR] Startup failed: {e}")
        show_error_dialog(f"EchoTray failed to start:\n\n    {e}")
    finally:
        print("\nExiting.")


if __name__ == "__main__":
    main()
