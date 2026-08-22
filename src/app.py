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

__version__ = "2.0.10"

import os
import pathlib
import gc
import signal
import subprocess
import sys
import threading
import time

import gi
from dotenv import load_dotenv

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import GLib, Gtk  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as appindicator  # noqa: E402

import helper_client
import whisper

load_dotenv()

# The real .env lives in the install dir (one level above app/), e.g.
# ~/.local/share/echotray/.env. load_dotenv() finds it by searching upward
# from this file; _DOTENV_PATH is the same location for writing settings.
_DOTENV_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / ".env"


def _write_env(key, value):
    """Set `key=value` in the app's .env file (creating/updating the line).

    Used by both the Setup window (for the selector settings) and the app (for
    the model size), so any setting changed in the UI persists across restarts.
    """
    path = _DOTENV_PATH
    try:
        lines = path.read_text().splitlines() if path.exists() else []
    except OSError:
        return
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    try:
        path.write_text("\n".join(lines) + "\n")
    except OSError:
        pass

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
    """Send a desktop notification and print to terminal.

    Notifications are marked transient (hint `transient:true`) so the
    notification daemon does NOT keep them in the notification tray/center -
    they show briefly and disappear, instead of stacking up to the top of the
    screen. A short timeout (5s) also auto-dismisses them.
    """
    print(f"[{summary}] {body}" if body else f"[{summary}]")
    args = [
        "notify-send", "-i", icon, "-u", urgency,
        "-t", "5000", "-h", "string:transient:true",
        summary,
    ]
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


# ── Setup window (non-modal, single Whisper Model section) ───────────────────

class _RoundedFrame(Gtk.Bin):
    """A container that draws its own rounded border with cairo.

    Theme CSS borders on Gtk.Frame are unreliable (themes override them), so we
    draw the border directly. This gives exact, theme-independent control over
    wall thickness and corner radius.
    """

    def __init__(self, border_width=5, radius=12, pad=10):
        super().__init__()
        self._border_width = border_width
        self._radius = radius
        self._pad = pad
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.connect("draw", self._draw)

    def _rounded_path(self, cr, x0, y0, w, h):
        r = self._radius
        x1, y1 = x0 + w, y0 + h
        cr.arc(x1 - r, y0 + r, r, -3.14159 / 2, 0)
        cr.arc(x1 - r, y1 - r, r, 0, 3.14159 / 2)
        cr.arc(x0 + r, y1 - r, r, 3.14159 / 2, 3.14159)
        cr.arc(x0 + r, y0 + r, r, 3.14159, 3 * 3.14159 / 2)
        cr.close_path()

    def _draw(self, _widget, cr):
        alloc = self.get_allocation()
        # Inset by half the border width so the stroke isn't clipped at the edge.
        bw = self._border_width
        pad = bw / 2.0
        w = alloc.width - bw
        h = alloc.height - bw
        self._rounded_path(cr, pad, pad, w, h)
        # Subtle fill so the group reads as a panel.
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.05)
        cr.fill_preserve()
        # The visible border (drawn after the fill, on the same path).
        cr.set_source_rgb(0.55, 0.57, 0.60)
        cr.set_line_width(self._border_width)
        cr.stroke()
        return False  # let children draw on top


class _StatusLight(Gtk.DrawingArea):
    """A small filled circle that is red (no model) or green (model loaded)."""

    def __init__(self, size=16):
        super().__init__()
        self.set_size_request(size, size)
        self._ok = False
        self.connect("draw", self._draw)

    def set_ok(self, ok):
        self._ok = bool(ok)
        self.queue_draw()

    def _draw(self, _widget, cr):
        color = (0.09, 0.63, 0.29) if self._ok else (0.86, 0.16, 0.16)
        alloc = self.get_allocation()
        cx = alloc.width / 2
        cy = alloc.height / 2
        r = min(alloc.width, alloc.height) / 2 - 2
        cr.set_source_rgb(*color)
        cr.arc(cx, cy, r, 0, 2 * 3.14159)
        cr.fill()


class SetupWindow(Gtk.Window):
    """Non-modal setup window with a Whisper Model section plus simple settings.

    The Whisper Model section shows a status light (green when a model is
    loaded, red when none), the currently loaded model, a dropdown to change
    model size, a Download button, and a live progress bar. Below it are three
    simple selector sections (no status lights): Language, Paste delay, and
    Max recording length. The model download is owned by the app and continues
    even if this window is closed.
    """

    _MODEL_SIZES = ["small", "base", "tiny", "medium", "large-v3"]

    def __init__(self, app):
        super().__init__(title="EchoTray Setup")
        self.app = app
        # Cap the window width so long hint text doesn't stretch it to the
        # length of the longest sentence. Height auto-sizes to fit content.
        self.set_default_size(420, -1)
        self.set_size_request(360, -1)
        self.set_border_width(12)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add(vbox)

        self.model_light = _StatusLight()
        sec, body = self._section_container("Whisper Model", self.model_light)

        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        grid.attach(Gtk.Label(label="Loaded:", xalign=1.0), 0, 0, 1, 1)
        self.model_label = Gtk.Label(label="none")
        self.model_label.set_xalign(0)
        grid.attach(self.model_label, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Change to:", xalign=1.0), 0, 1, 1, 1)
        self.model_combo = Gtk.ComboBoxText()
        for s in self._MODEL_SIZES:
            self.model_combo.append_text(s)
        self.model_combo.set_active(0)
        grid.attach(self.model_combo, 1, 1, 1, 1)

        self.download_btn = Gtk.Button(label="Download")
        self.download_btn.connect("clicked", self._on_download)
        grid.attach(self.download_btn, 2, 1, 1, 1)

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_text("")
        grid.attach(self.progress, 1, 2, 2, 1)
        body.pack_start(grid, False, False, 0)
        vbox.pack_start(sec, False, False, 0)

        # ── Language section (no status light) ───────────────────────────────
        sec, body = self._section_container("Language")
        lang_grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        lang_grid.attach(Gtk.Label(label="Dictation language:", xalign=1.0), 0, 0, 1, 1)
        self.lang_combo = Gtk.ComboBoxText()
        # First entry is auto-detect (empty WHISPER_LANGUAGE); the rest are
        # common Whisper language codes.
        self._LANG_OPTIONS = [
            ("Auto-detect", ""),
            ("English", "en"),
            ("French", "fr"),
            ("German", "de"),
            ("Spanish", "es"),
            ("Italian", "it"),
            ("Portuguese", "pt"),
            ("Dutch", "nl"),
        ]
        for label, _code in self._LANG_OPTIONS:
            self.lang_combo.append_text(label)
        self._set_combo_from_value(self.lang_combo, whisper.LANGUAGE or "", self._LANG_OPTIONS, 1)
        self.lang_combo.connect("changed", self._on_lang_changed)
        lang_grid.attach(self.lang_combo, 1, 0, 1, 1)
        body.pack_start(lang_grid, False, False, 0)
        vbox.pack_start(sec, False, False, 0)

        # ── Paste delay section (no status light) ─────────────────────────────
        sec, body = self._section_container("Paste delay")
        paste_grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        paste_grid.attach(Gtk.Label(label="Delay (ms):", xalign=1.0), 0, 0, 1, 1)
        self.paste_spin = Gtk.SpinButton.new_with_range(0, 2000, 50)
        self.paste_spin.set_value(PASTE_DELAY_MS)
        self.paste_spin.set_numeric(True)
        self.paste_spin.connect("value-changed", self._on_paste_delay_changed)
        paste_grid.attach(self.paste_spin, 1, 0, 1, 1)
        hint = Gtk.Label(label="Delay between copying the text and pasting it. Increase if the paste comes out blank.")
        hint.set_line_wrap(True)
        hint.set_max_width_chars(40)
        hint.set_xalign(0)
        paste_grid.attach(hint, 1, 1, 1, 1)
        body.pack_start(paste_grid, False, False, 0)
        vbox.pack_start(sec, False, False, 0)

        # ── Max recording length section (no status light) ──────────────────
        sec, body = self._section_container("Max recording length")
        rec_grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        rec_grid.attach(Gtk.Label(label="Seconds:", xalign=1.0), 0, 0, 1, 1)
        self.rec_spin = Gtk.SpinButton.new_with_range(10, 3600, 10)
        self.rec_spin.set_value(whisper.MAX_RECORDING_SECONDS)
        self.rec_spin.set_numeric(True)
        self.rec_spin.connect("value-changed", self._on_rec_length_changed)
        rec_grid.attach(self.rec_spin, 1, 0, 1, 1)
        hint = Gtk.Label(label="How long a recording can run before it auto-stops.")
        hint.set_line_wrap(True)
        hint.set_max_width_chars(40)
        hint.set_xalign(0)
        rec_grid.attach(hint, 1, 1, 1, 1)
        body.pack_start(rec_grid, False, False, 0)
        vbox.pack_start(sec, False, False, 0)

        self._poll_id = GLib.timeout_add(500, self._poll)
        self.connect("destroy", self._on_destroy)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _section_container(self, title, light=None):
        """Return a (frame, body) where body holds the section's fields.

        Uses _RoundedFrame, which draws its own border with cairo - exact,
        theme-independent wall thickness and rounded corners (Gtk.Frame CSS
        borders are overridden by themes and don't render reliably). `light` is
        optional; pass None for a section with no status light.
        """
        frame = _RoundedFrame(border_width=2, radius=12)

        # Inner vertical box: header row (status light + title) above the body.
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        header = self._section_header(title, light)
        header.set_margin_top(8)
        header.set_margin_start(12)
        header.set_margin_end(12)
        inner.pack_start(header, False, False, 0)

        # Body: the caller packs the section's fields here, inside the frame.
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.set_margin_start(12)
        body.set_margin_end(12)
        body.set_margin_bottom(12)
        inner.pack_start(body, False, False, 0)

        frame.add(inner)
        return frame, body

    def _section_header(self, title, light=None):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if light is not None:
            box.pack_start(light, False, False, 0)
        lbl = Gtk.Label(label=title, xalign=0.0)
        lbl.set_hexpand(True)
        box.pack_start(lbl, False, False, 0)
        return box

    def _on_download(self, _btn):
        size = self.model_combo.get_active_text() or "small"
        self.app._start_model_download(size)

    # ── settings persistence ─────────────────────────────────────────────────
    def _set_combo_from_value(self, combo, value, options, default_index):
        """Select the combo entry whose code matches `value` (or the default)."""
        for i, (_label, code) in enumerate(options):
            if code == value:
                combo.set_active(i)
                return
        combo.set_active(default_index)

    def _on_lang_changed(self, combo):
        code = self._LANG_OPTIONS[combo.get_active()][1] if combo.get_active() >= 0 else ""
        _write_env("WHISPER_LANGUAGE", code)
        # Apply live so the next transcription uses the new language.
        whisper.LANGUAGE = code or None

    def _on_paste_delay_changed(self, spin):
        value = int(spin.get_value())
        _write_env("PASTE_DELAY_MS", str(value))
        global PASTE_DELAY_MS
        PASTE_DELAY_MS = value

    def _on_rec_length_changed(self, spin):
        value = int(spin.get_value())
        _write_env("MAX_RECORDING_SECONDS", str(value))
        whisper.MAX_RECORDING_SECONDS = value

    # ── polling ──────────────────────────────────────────────────────────────
    def _poll(self):
        # Green if ANY model is downloaded; show progress if a download is active.
        downloading = self.app._download_progress["active"]
        if downloading:
            frac = self.app._download_progress["fraction"]
            self.progress.set_fraction(frac)
            self.progress.set_text(f"{int(frac * 100)}%")
            self.download_btn.set_sensitive(False)
        else:
            self.progress.set_fraction(0)
            self.progress.set_text("")
            self.download_btn.set_sensitive(True)
        downloaded = whisper.downloaded_model_sizes()
        model_ready = bool(downloaded)
        self.model_light.set_ok(model_ready)
        # Show the downloaded model(s); prefer the configured/active size.
        if model_ready:
            shown = self.app.model_size if self.app.model_size in downloaded else downloaded[0]
            self.model_label.set_text(shown)
        else:
            self.model_label.set_text("none")

        return True  # keep polling

    def _on_destroy(self, _widget):
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None
        # Clear the app's cached reference so the next Setup menu click builds a
        # fresh window. Without this, reopening after close calls show_all()/
        # present() on a destroyed widget, which renders a tiny empty square.
        if self.app._setup_window is self:
            self.app._setup_window = None


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
        self._setup_window = None
        self._about_dialog = None
        # Last icon path, so the 500ms poll timer only calls set_icon_full()
        # when the icon actually changes (set_icon_full reloads the icon from
        # disk and leaks memory if called every tick).
        self._last_icon = None
        # Monotonic deadline for a short post-idle re-assert window (see
        # _update_icon). Bounded, so the idle memory leak stays fixed.
        self._idle_assert_until = 0.0
        # App-owned model download state, polled by the setup window. The
        # download is owned by the app (not the window) so it continues even if
        # the window closes.
        self.model_size = whisper.MODEL_SIZE
        self._download_thread = None
        self._download_result = {"ok": False, "error": None}
        self._download_progress = {"active": False, "fraction": 0.0, "bytes": 0, "size": None}

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

        # Setup - change the Whisper model size (or install it on first run).
        setup_item = Gtk.MenuItem(label="Setup...")
        setup_item.connect("activate", self._on_setup)
        menu.append(setup_item)

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

    def _on_setup(self, _widget):
        """Open the non-modal setup window (single Whisper Model section).

        The window is non-modal so the tray stays usable. The model download is
        owned by the app and continues even if the window is closed.
        """
        if self._setup_window is None:
            self._setup_window = SetupWindow(self)
        self._setup_window.show_all()
        self._setup_window.present()

    def _start_model_download(self, size):
        """Start (or restart) a background model download owned by the app.

        The download is owned by the app, not the setup window, so it continues
        even if the window is closed. Progress is written to
        self._download_progress for the setup window to poll. When it finishes,
        the model is loaded and the tray flips to ready.
        """
        if self._download_thread is not None and self._download_thread.is_alive():
            return  # already downloading
        self.model_size = size
        # Persist the chosen size so it survives a restart (otherwise main()
        # reloads the old configured size on next launch).
        _write_env("MODEL_SIZE", size)
        whisper.MODEL_SIZE = size
        self._download_result = {"ok": False, "error": None}
        self._download_progress = {"active": True, "fraction": 0.0, "bytes": 0, "size": size}

        def _job():
            try:
                def cb(fraction, bytes_done):
                    self._download_progress["fraction"] = fraction
                    self._download_progress["bytes"] = bytes_done
                whisper.download_model(size, progress_cb=cb)
                self._download_result["ok"] = True
            except Exception as e:  # noqa: BLE001
                self._download_result["error"] = e
            finally:
                self._download_progress["active"] = False
                self._download_progress["fraction"] = 1.0
                if self._download_result["ok"]:
                    # Load the freshly downloaded model and enable the app.
                    try:
                        model = whisper.load_model(size)
                    except Exception as e:  # noqa: BLE001
                        print(f"[ERROR] Model load failed: {e}")
                        GLib.idle_add(notify, "EchoTray", f"Model load failed: {e}", "audio-input-microphone", "critical")
                        return
                    GLib.idle_add(self._activate_model, model)
                else:
                    err = self._download_result["error"]
                    print(f"[ERROR] Model download failed: {err}")
                    GLib.idle_add(notify, "EchoTray", f"Model download failed: {err}", "audio-input-microphone", "critical")

        self._download_thread = threading.Thread(target=_job, daemon=True)
        self._download_thread.start()

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
        self._last_icon = ICON_DISABLED
        self.status_item.set_label("Status: waiting for model")
        try:
            self.toggle_item.set_sensitive(False)
        except Exception:
            pass

    def set_ready(self):
        """Post-model state: green idle icon, Start Recording enabled."""
        self.state = "IDLE"
        self.indicator.set_icon_full(ICON_IDLE, "Idle")
        self._last_icon = ICON_IDLE
        self.status_item.set_label("Status: Idle")
        try:
            self.toggle_item.set_sensitive(True)
            self.toggle_item.set_label("Start recording")
        except Exception:
            pass

    def set_idle(self):
        self.state = "IDLE"
        # libayatana-appindicator only emits the NewIcon DBus signal when the
        # icon NAME actually changes, so re-asserting the same green icon is a
        # silent no-op (and the GNOME extension's own equality check can also
        # dedupe a green->green update against its stale cache). Toggle through
        # a distinct icon first so the final green set is always a genuine name
        # change the tray can't collapse away after a fast no-speech stop. Both
        # calls run in the same main-loop tick, so no grey flicker is visible.
        self.indicator.set_icon_full(ICON_DISABLED, "Idle")
        self.indicator.set_icon_full(ICON_IDLE, "Idle")
        self._last_icon = ICON_IDLE
        # Keep the short post-idle re-assert window as a belt-and-suspenders
        # self-heal (bounded, so the idle memory leak stays fixed).
        self._idle_assert_until = time.monotonic() + 2.0
        self.status_item.set_label("Status: Idle")
        try:
            self.toggle_item.set_label("Start recording")
        except Exception:
            pass

    def set_recording(self):
        self.state = "RECORDING"
        self.indicator.set_icon_full(ICON_RECORDING, "Recording")
        self._last_icon = ICON_RECORDING
        self.status_item.set_label("Status: Recording...")
        try:
            self.toggle_item.set_label("Stop recording")
        except Exception:
            pass

    def set_transcribing(self):
        self.state = "TRANSCRIBING"
        self.indicator.set_icon_full(ICON_PROCESSING, "Transcribing")
        self._last_icon = ICON_PROCESSING
        self.status_item.set_label("Status: Transcribing...")

    def _on_about(self, _widget):
        # Cache the dialog so repeated clicks reuse it instead of building a
        # new one each time (and leaking the old one).
        if self._about_dialog is not None:
            self._about_dialog.present()
            return
        dialog = Gtk.AboutDialog()
        self._about_dialog = dialog
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
        dialog.connect("response", self._on_about_response)
        dialog.show()
        dialog.present()
        # The About dialog's comments text view grabs keyboard focus and shows a
        # blinking text cursor even though nothing is editable. Clear focus when
        # it appears so no cursor is shown.
        dialog.connect("show", lambda d: d.set_focus(None))

    def _on_about_response(self, dialog, _response):
        # Clear the cached reference so the next About click builds a fresh
        # dialog (a destroyed widget can't be re-presented).
        if self._about_dialog is dialog:
            self._about_dialog = None
        dialog.destroy()

    def _on_quit(self, _widget):
        # Stop any active recorder before quitting so the audio device is
        # released cleanly (a wedged device is handled by recorder.stop()'s
        # bounded teardown). Guard against None so quit always works.
        recorder = self.recorder
        self.recorder = None
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                pass
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
        # Re-assert the tray icon from state every 500ms. The desktop tray can
        # occasionally drop/revert an icon; re-asserting here self-heals the
        # glitch (e.g. the icon reverting to idle-green while still recording).
        GLib.timeout_add(500, self._update_icon)
        # The "Ready" notification now fires from _load_and_enable() once the
        # model is actually loaded (not here), so the tray can start disabled.
        print("\nEchoTray running (tray icon visible).\n")
        self.loop.run()

    def _update_icon(self):
        """Re-assert the tray icon from the current state (poll timer).

        Only calls set_icon_full() when the icon actually changes: set_icon_full
        reloads the icon from disk on every call, so calling it every 500ms
        leaks memory continuously (the app's RSS drifts up while idle).

        The transient states (RECORDING/TRANSCRIBING) re-assert every tick so a
        dropped icon self-heals immediately. IDLE re-asserts only during a short
        post-idle window (set by set_idle) so a fast no-speech stop can't leave
        the tray stuck on amber/red; after that window it goes quiet to keep the
        idle memory leak fixed.
        """
        if self.state == "RECORDING":
            want = ICON_RECORDING
        elif self.state == "TRANSCRIBING":
            want = ICON_PROCESSING
        elif self.state == "IDLE":
            # No model loaded yet -> keep the grey disabled icon, not green.
            want = ICON_DISABLED if self.model is None else ICON_IDLE
        else:
            want = None
        if want is None:
            return True
        # Re-assert if the icon changed, OR if we're in a transient state, OR
        # during the short post-idle window. Otherwise stay quiet (idle leak fix).
        transient = self.state in ("RECORDING", "TRANSCRIBING")
        in_idle_window = self.state == "IDLE" and time.monotonic() < self._idle_assert_until
        if want != self._last_icon or transient or in_idle_window:
            self.indicator.set_icon_full(want, "Recording" if want == ICON_RECORDING else ("Transcribing" if want == ICON_PROCESSING else ("Waiting for model" if want == ICON_DISABLED else "Idle")))
            self._last_icon = want
        return True  # keep the timer running


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
        # Force LINE buffering on stdout/stderr. When redirected to a file,
        # Python block-buffers stdout (~8KB), so print() diagnostics pile up in
        # the buffer and never reach the log until the process exits. This
        # flushes on every newline so the log is actually useful for debugging.
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(line_buffering=True)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
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
    # to install it via the Setup menu.
    app = DictationApp(model=None)
    app.set_disabled()

    # Auto-load ANY already-downloaded model (not just the configured size), so
    # a cached model is detected and the tray goes ready without user action.
    downloaded = whisper.downloaded_model_sizes()
    if downloaded:
        size = whisper.MODEL_SIZE if whisper.MODEL_SIZE in downloaded else downloaded[0]
        print(f"Model '{size}' is cached - loading in the background...")
        def _load():
            try:
                model = whisper.load_model(size)
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
