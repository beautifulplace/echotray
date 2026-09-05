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

__version__ = "2.3.2"

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

# Try to release freed glibc heap arenas back to the OS after dictation; this is
# available on glibc/Linux. If ctypes is missing, flushing is a no-op.
_ = ctypes = None
try:
    import ctypes as _ctypes
    ctypes = _ctypes
except ImportError:
    pass

def _flush_memory():
    """Return freed memory back to the OS and collect Python garbage.

    Python/glibc keeps freed heap arenas in the process, so RSS stays at the
    high-water mark even after the audio buffer and transcription transient
    objects are deleted. This forces a GC cycle and asks glibc to release
    unused pages, which is what makes the process RSS actually drop.
    """
    gc.collect()
    if ctypes is not None:
        try:
            ctypes.CDLL(None).malloc_trim(0)
        except Exception:
            pass

def _log_rss(label=""):
    """Print current RSS in kB if /proc/self/status is readable."""
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = line.split()[1]
                    print(f"[RSS] {label} {rss} kB" if label else f"[RSS] {rss} kB")
                    break
    except Exception:
        pass

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import GLib, Gtk, Gdk  # noqa: E402
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

PASTE_DELAY_MS = whisper.env_int("PASTE_DELAY_MS", 100)

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

    Notifications are marked transient (hint `string:transient:true`) so the
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


# ── Control styling ───────────────────────────────────────────────────────────

# A small set of control-level rules (rounded, padded buttons and entries) that
# apply on top of whatever system theme is active. We deliberately do NOT set
# window/background/foreground colors, so the app follows the user's light or
# dark system theme. Grouped controls use Gtk.ListBox panels (theme background,
# no border) to match the About window.
_THEME_CSS = b"""
button {
    background-image: none;
    border-radius: 4px;
    padding: 6px 12px;
}
entry {
    border-radius: 4px;
    padding: 6px;
}
.heading {
    font-weight: bold;
}
.version-pill {
    background-color: #2ec27e;
    color: #ffffff;
    border-radius: 999px;
    padding: 2px 10px;
}
.monospace {
    font-family: monospace;
}
"""


def _apply_theme():
    """Load the control-styling CSS provider, guarded for headless launch.

    GTK3 has no add_provider_for_display (that is GTK4). The screen-based API
    is the only option, but Gdk.Screen.get_default() returns None on Wayland.
    Get the screen from the display instead — Gdk.Display.get_default_screen()
    returns a valid screen on both X11 and Wayland. Guard against a missing
    display (headless) so the app still launches without a screen.
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(_THEME_CSS)
    display = Gdk.Display.get_default()
    if display is not None:
        screen = display.get_default_screen()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


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


def _monospace_font():
    """Return a monospace Pango font description for the debug text view."""
    from gi.repository import Pango
    return Pango.FontDescription("monospace 10")


def _debug_info():
    """Return a copyable block of version/config details plus the app log.

    Mirrors the Camera app's "Debugging Information" page: app version, library
    versions, current configuration, and the tail of the app log, in a
    monospace block the user can select, copy, or save.
    """
    def _ver(dist):
        # Read the installed version from package metadata, NOT by importing
        # the module. Importing faster-whisper pulls in ctranslate2/onnxruntime,
        # which would make opening the About window slow even though the debug
        # page is never shown.
        try:
            from importlib.metadata import version
            return version(dist)
        except Exception:
            return "unknown"

    lines = [
        "EchoTray " + __version__,
        "",
        "Libraries:",
        "  faster-whisper  " + _ver("faster-whisper"),
        "  ctranslate2     " + _ver("ctranslate2"),
        "  GTK             " + ".".join(str(x) for x in (Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version())),
        "",
        "Configuration:",
        "  MODEL_SIZE       " + whisper.MODEL_SIZE,
        "  COMPUTE_TYPE     " + whisper.COMPUTE_TYPE,
        "  LANGUAGE         " + (whisper.LANGUAGE or "(auto-detect)"),
        "  CPU_THREADS      " + str(whisper.CPU_THREADS),
        "  LOG_PATH         " + str(_LOG_PATH),
    ]

    # Append the tail of the app log (last ~200 lines) if it exists.
    try:
        if _LOG_PATH.exists():
            log_text = _LOG_PATH.read_text(errors="replace")
            tail = log_text.strip().splitlines()[-200:]
            lines.append("")
            lines.append("Log (last %d lines):" % len(tail))
            lines.extend(tail)
    except OSError:
        pass

    return "\n".join(lines)


class AboutWindow(Gtk.Window):
    """Non-modal About window in the GNOME/Adwaita style.

    Large centered app icon, bold title, a green version pill, and the app
    description as a plain (non-button) row. Below it: Website (a button that
    opens the repo, tooltip shows the URL) and Troubleshooting (slides over to
    debugging info).
    """

    REPO_URL = "https://github.com/beautifulplace/echotray"

    def __init__(self, app):
        super().__init__(title="About EchoTray")
        self.app = app
        self.set_default_size(360, -1)
        self.set_border_width(0)
        # Non-resizable: removes the maximize button (the DIALOG type hint that
        # used to do this also made Mutter keep the window above normal windows,
        # so we avoid it and just disable resizing instead).
        self.set_resizable(False)

        # Header bar with a close button and a back button (shown on the
        # Troubleshooting page).
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title("About EchoTray")
        self._back_btn = Gtk.Button()
        self._back_btn.set_image(
            Gtk.Image.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON))
        self._back_btn.set_tooltip_text("Back")
        self._back_btn.connect("clicked", self._on_back)
        # show_all() would reveal this button on the main page; keep it hidden
        # until a subpage is pushed (set_visible(True) still works explicitly).
        self._back_btn.set_no_show_all(True)
        hb.pack_start(self._back_btn)
        self.set_titlebar(hb)

        # Stack with a slide transition between pages. Navigation is a simple
        # push/pop stack so the back button returns one level at a time
        # (main -> troubleshooting -> debugging info).
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self._stack.set_transition_duration(200)
        self.add(self._stack)

        self._stack.add_named(self._build_main_page(), "main")
        self._stack.add_named(self._build_troubleshooting_page(), "troubleshooting")
        self._stack.add_named(self._build_debug_page(), "debug")

        self._nav_history = []  # stack of page names, for back navigation
        self._back_btn.set_visible(False)
        self.connect("delete-event", self._on_close)

    def _on_close(self, *_args):
        # Reset to the main page so reopening the window starts fresh instead
        # of landing on whatever subpage (Troubleshooting/Debug) was last open.
        self._nav_history = []
        self._stack.set_visible_child_name("main")
        self._back_btn.set_visible(False)
        self.hide()
        return True  # stop the default destroy; the window is reused

    # ── page navigation ────────────────────────────────────────────────────────
    def _push_page(self, name):
        self._nav_history.append(self._stack.get_visible_child_name())
        self._stack.set_visible_child_name(name)
        self._back_btn.set_visible(True)

    def _on_back(self, _btn):
        if self._nav_history:
            prev = self._nav_history.pop()
            self._stack.set_visible_child_name(prev)
        else:
            self._stack.set_visible_child_name("main")
        self._back_btn.set_visible(bool(self._nav_history))

    def _show_troubleshooting(self, _row):
        self._push_page("troubleshooting")

    def _show_debug(self, _row):
        self._push_page("debug")

    # ── main page ──────────────────────────────────────────────────────────────
    def _build_main_page(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Header: large icon + bold title + version pill.
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header.set_halign(Gtk.Align.CENTER)
        header.set_margin_top(24)
        header.set_margin_bottom(16)
        header.set_margin_start(24)
        header.set_margin_end(24)

        try:
            from gi.repository import GdkPixbuf
            pb = GdkPixbuf.Pixbuf.new_from_file(ICON_IDLE)
            pb = pb.scale_simple(96, 96, GdkPixbuf.InterpType.BILINEAR)
            img = Gtk.Image.new_from_pixbuf(pb)
        except Exception:
            img = Gtk.Image.new_from_icon_name("audio-input-microphone", Gtk.IconSize.DIALOG)
        img.set_halign(Gtk.Align.CENTER)
        header.pack_start(img, False, False, 0)

        title = Gtk.Label(label="EchoTray", xalign=0.5)
        title.set_halign(Gtk.Align.CENTER)
        title.get_style_context().add_class("title")
        header.pack_start(title, False, False, 0)

        pill = Gtk.Label(label=__version__, xalign=0.5)
        pill.set_halign(Gtk.Align.CENTER)
        pill.get_style_context().add_class("version-pill")
        header.pack_start(pill, False, False, 0)

        vbox.pack_start(header, False, False, 0)

        # The app description as a plain (non-button) row, no icon (the icon is
        # already shown at the top).
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.set_margin_start(12)
        listbox.set_margin_end(12)

        listbox.add(self._plain_row(
            "Speech-to-Text Dictation",
            "Click the tray microphone to start recording, click again to stop - the transcript is pasted at your cursor.",
        ))

        vbox.pack_start(listbox, False, False, 0)

        # Standard About rows. Each row is a full-width clickable row (hover
        # highlight, single-click activation, no persistent selection).
        rows = Gtk.ListBox()
        rows.set_selection_mode(Gtk.SelectionMode.NONE)
        rows.set_activate_on_single_click(True)
        rows.connect("row-activated", self._on_row_activated)
        rows.set_margin_start(12)
        rows.set_margin_end(12)
        rows.set_margin_top(12)
        rows.set_margin_bottom(12)

        # Website — opens the repo; tooltip shows the URL.
        rows.add(self._button_row("Website", "website", chevron=True, tooltip=self.REPO_URL))

        # Troubleshooting — slides over to a "how to use debugging info" page.
        rows.add(self._button_row("Troubleshooting", "troubleshooting", chevron=True))

        vbox.pack_start(rows, False, False, 0)

        return vbox

    # ── Troubleshooting page ───────────────────────────────────────────────────
    def _build_troubleshooting_page(self):
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        title = Gtk.Label(label="Troubleshooting", xalign=0.0)
        title.get_style_context().add_class("title")
        box.pack_start(title, False, False, 0)

        body = Gtk.Label(
            label=("If EchoTray is not working as expected, the debugging "
                   "information below can help diagnose the problem. It lists "
                   "the app version, the library versions, and the current "
                   "configuration.\n\n"
                   "You can copy it and include it when reporting an issue."),
            xalign=0.0, wrap=True)
        body.set_line_wrap(True)
        body.set_halign(Gtk.Align.FILL)
        box.pack_start(body, False, False, 0)

        btn = Gtk.Button(label="View debugging information")
        btn.set_halign(Gtk.Align.START)
        btn.connect("clicked", self._show_debug)
        box.pack_start(btn, False, False, 0)

        scroller.add(box)
        return scroller

    # ── Debugging Information page ─────────────────────────────────────────────
    def _build_debug_page(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vbox.set_margin_top(16)
        vbox.set_margin_bottom(16)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)

        title = Gtk.Label(label="Debugging Information", xalign=0.0)
        title.get_style_context().add_class("title")
        vbox.pack_start(title, False, False, 0)

        # The log content in a scrollable, read-only monospace text view.
        # A TextView (unlike a selectable Gtk.Label) shows plain text with no
        # blue selection highlight by default; the Copy button copies the full
        # text regardless of selection.
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)

        self._debug_text = _debug_info()
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_wrap_mode(Gtk.WrapMode.NONE)
        view.get_buffer().set_text(self._debug_text)
        view.override_font(_monospace_font())
        scroller.add(view)
        vbox.pack_start(scroller, True, True, 0)

        # Copy and Save As buttons.
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)

        copy_btn = Gtk.Button(label="Copy")
        copy_btn.connect("clicked", self._copy_debug)
        btn_row.pack_start(copy_btn, False, False, 0)

        save_btn = Gtk.Button(label="Save As…")
        save_btn.connect("clicked", self._save_debug)
        btn_row.pack_start(save_btn, False, False, 0)

        vbox.pack_start(btn_row, False, False, 0)

        return vbox

    def _copy_debug(self, _btn):
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(self._debug_text, -1)

    def _save_debug(self, _btn):
        dialog = Gtk.FileChooserDialog(
            title="Save Debugging Information",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE, Gtk.ResponseType.ACCEPT,
        )
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name("echotray-debug.txt")
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            path = dialog.get_filename()
            try:
                with open(path, "w") as f:
                    f.write(self._debug_text)
            except OSError as e:
                print(f"[ABOUT] Failed to save debug info: {e}")
        dialog.destroy()

    # ── row builders ──────────────────────────────────────────────────────────
    def _row_box(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        return box

    def _plain_row(self, heading, blurb):
        """A non-interactive row: heading/blurb, no icon, no chevron, no hover."""
        row = Gtk.ListBoxRow()
        row.set_activatable(False)  # no hover/selection highlight
        row.set_selectable(False)
        box = self._row_box()

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        head = Gtk.Label(label=heading, xalign=0.5)
        head.set_halign(Gtk.Align.CENTER)
        head.get_style_context().add_class("heading")
        text.pack_start(head, False, False, 0)
        body = Gtk.Label(label=blurb, xalign=0.5, wrap=True)
        body.set_line_wrap(True)
        body.set_max_width_chars(40)
        body.set_halign(Gtk.Align.CENTER)
        body.get_style_context().add_class("dim-label")
        text.pack_start(body, False, False, 0)
        box.pack_start(text, True, True, 0)

        row.add(box)
        return row

    def _button_row(self, label, action, chevron=False, tooltip=None):
        """A full-width clickable row (the whole row is the button).

        The row itself is activatable (hover highlight + single-click), and the
        action is dispatched via the ListBox 'row-activated' signal, keyed by
        the `action` string.
        """
        row = Gtk.ListBoxRow()
        row.set_activatable(True)
        row._action = action  # noqa: SLF001
        box = self._row_box()
        lbl = Gtk.Label(label=label, xalign=0.0)
        box.pack_start(lbl, True, True, 0)
        if chevron:
            chev = Gtk.Label(label="›", xalign=0.5)
            chev.get_style_context().add_class("dim-label")
            box.pack_start(chev, False, False, 0)
        if tooltip:
            row.set_tooltip_text(tooltip)
        row.add(box)
        return row

    def _on_row_activated(self, _listbox, row):
        action = getattr(row, "_action", None)
        if action == "website":
            self._open_website()
        elif action == "troubleshooting":
            self._show_troubleshooting(None)

    def _open_website(self, *_args):
        try:
            Gtk.show_uri_on_window(self, self.REPO_URL, Gdk.CURRENT_TIME)
        except Exception as e:  # noqa: BLE001
            print(f"[ABOUT] Failed to open website: {e}")


class SetupWindow(Gtk.Window):
    """Non-modal setup window with a Whisper Model section plus simple settings.

    The Whisper Model section shows a status light (green when a model is
    loaded, red when none), the currently loaded model, a dropdown to change
    model size, a Download button, and a live progress bar. Below it are three
    simple selector sections (no status lights): Language, Paste delay, and
    Max recording length. The model download is owned by the app and continues
    even if this window is closed.
    """

    # Model sizes smallest -> largest, with download size for the dropdown's
    # right-justified size column. Sizes are the total download (all files) from
    # the Systran/faster-whisper-* HuggingFace repos. Keys match whisper._MODEL_REPOS.
    _MODEL_SIZES = [
        ("Tiny", "78 MB", "tiny"),
        ("Base", "148 MB", "base"),
        ("Small", "486 MB", "small"),
        ("Medium", "1.5 GB", "medium"),
        ("Large-v3", "3.1 GB", "large-v3"),
    ]

    def __init__(self, app):
        super().__init__(title="EchoTray Setup")
        self.app = app
        # Cap the window width so long hint text doesn't stretch it to the
        # length of the longest sentence. Height auto-sizes to fit content.
        self.set_default_size(420, -1)
        self.set_size_request(360, -1)
        self.set_border_width(0)
        # Non-resizable: removes the maximize button and fixes the window size
        # (minimize + close only, matching the About window).
        self.set_resizable(False)

        # Header bar with a close button (Adwaita-style).
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title("EchoTray Setup")
        self.set_titlebar(hb)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)
        self.add(vbox)

        # ── Whisper Model box ─────────────────────────────────────────────────
        sec, body = self._section("Whisper Model")

        # Line 1: status light + "Loaded: <model>" (or "Select model"), with a
        # load/unload toggle at the far end.
        self.model_light = _StatusLight()
        self.model_label = Gtk.Label(label="Select model")
        self.model_label.set_xalign(0.0)
        self.model_label.set_hexpand(True)
        self.load_toggle = Gtk.Switch()
        self.load_toggle.set_valign(Gtk.Align.CENTER)
        self.load_toggle.connect("notify::active", self._on_load_toggle)
        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row1.pack_start(self.model_light, False, False, 0)
        row1.pack_start(self.model_label, True, True, 0)
        row1.pack_start(self.load_toggle, False, False, 0)
        body.pack_start(row1, False, False, 0)

        # Line 2: model dropdown (name left, download size right-justified),
        # then Download and Delete buttons on the same line.
        self.model_combo = self._build_model_combo()
        self.download_btn = Gtk.Button(label="Download")
        self.download_btn.connect("clicked", self._on_download)
        self.delete_btn = Gtk.Button(label="Delete")
        self.delete_btn.connect("clicked", self._on_delete_model)
        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row2.pack_start(self.model_combo, True, True, 0)
        row2.pack_start(self.download_btn, False, False, 0)
        row2.pack_start(self.delete_btn, False, False, 0)
        body.pack_start(row2, False, False, 0)

        # Progress bar spans the full width of the box.
        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_text("")
        self.progress.set_hexpand(True)
        body.pack_start(self.progress, False, False, 0)

        vbox.pack_start(sec, False, False, 0)

        # ── Settings box ──────────────────────────────────────────────────────
        sec, body = self._section("Settings")

        # Dictation language: label left, dropdown right.
        self.lang_combo = Gtk.ComboBoxText()
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
        row = self._setting_row("Dictation language", self.lang_combo)
        body.pack_start(row, False, False, 0)

        # Paste delay: label left, spin button right.
        self.paste_spin = Gtk.SpinButton.new_with_range(0, 2000, 50)
        self.paste_spin.set_value(PASTE_DELAY_MS)
        self.paste_spin.set_numeric(True)
        self.paste_spin.connect("value-changed", self._on_paste_delay_changed)
        row = self._setting_row("Paste delay (ms)", self.paste_spin)
        body.pack_start(row, False, False, 0)

        # Max recording length: label left, spin button right.
        self.rec_spin = Gtk.SpinButton.new_with_range(10, 3600, 10)
        self.rec_spin.set_value(whisper.MAX_RECORDING_SECONDS)
        self.rec_spin.set_numeric(True)
        self.rec_spin.connect("value-changed", self._on_rec_length_changed)
        row = self._setting_row("Max recording length (s)", self.rec_spin)
        body.pack_start(row, False, False, 0)

        vbox.pack_start(sec, False, False, 0)

        self._poll_id = GLib.timeout_add(500, self._poll)
        self.connect("destroy", self._on_destroy)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _section(self, title, light=None):
        """Return a (outer, body) where the heading sits OUTSIDE the box.

        A left-aligned heading above a box that holds the settings. The box is a
        Gtk.ListBox (theme panel background, no border), matching the About
        window's boxes exactly. `light` is an optional status light shown next
        to the heading.
        """
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        # Heading row, outside the box, left-aligned.
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if light is not None:
            header.pack_start(light, False, False, 0)
        lbl = Gtk.Label(label=title, xalign=0.0)
        lbl.get_style_context().add_class("heading")
        header.pack_start(lbl, False, False, 0)
        outer.pack_start(header, False, False, 0)

        # The box: a ListBox gives the theme's panel background (dark in a dark
        # theme) with no border, exactly like the About window's boxes.
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.set_margin_start(12)
        body.set_margin_end(12)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        row.add(body)
        listbox.add(row)
        outer.pack_start(listbox, False, False, 0)

        return outer, body

    def _setting_row(self, label, control):
        """A single setting row: label on the left, control on the right.

        Matches Easy Effects' "Spectrum frame rate cap" style — the label is
        left-aligned and the control (dropdown/spin button) is right-aligned on
        the same line.
        """
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        lbl = Gtk.Label(label=label, xalign=0.0)
        lbl.set_hexpand(True)
        row.pack_start(lbl, True, True, 0)
        row.pack_start(control, False, False, 0)
        return row

    def _build_model_combo(self):
        """Build the model dropdown: name left, download size right-justified.

        Uses a Gtk.ComboBox with a custom cell renderer so the size column is
        right-aligned (Gtk.ComboBoxText can't justify a second column).
        """
        store = Gtk.ListStore(str, str, str)  # (display name, size string, key)
        for name, size_str, key in self._MODEL_SIZES:
            store.append([name, size_str, key])
        combo = Gtk.ComboBox(model=store)

        name_cell = Gtk.CellRendererText()
        combo.pack_start(name_cell, True)
        combo.add_attribute(name_cell, "text", 0)

        size_cell = Gtk.CellRendererText()
        size_cell.set_property("xalign", 1.0)  # right-justify the size
        size_cell.set_property("foreground", "#888888")
        combo.pack_start(size_cell, False)
        combo.add_attribute(size_cell, "text", 1)

        # Select the configured size by default.
        configured = whisper.MODEL_SIZE
        for i, (_name, _size_str, key) in enumerate(self._MODEL_SIZES):
            if key == configured:
                combo.set_active(i)
                break
        else:
            combo.set_active(0)
        return combo

    def _selected_size(self):
        """Return the size key currently selected in the model dropdown."""
        idx = self.model_combo.get_active()
        if idx < 0:
            return "small"
        return self._MODEL_SIZES[idx][2]

    def _on_load_toggle(self, switch, _pspec):
        """Load or unload the model selected in the dropdown.

        Toggling ON loads the selected size (downloading first if needed);
        toggling OFF unloads whatever is currently loaded.
        """
        if switch.get_active():
            size = self._selected_size()
            if size in whisper.downloaded_model_sizes():
                self.app._start_model_load(size)
            else:
                self.app._start_model_download(size)
        else:
            # Unload the currently loaded model and drop to disabled.
            if self.app.model is not None:
                whisper.unload_model(self.app.model)
                self.app.model = None
                self.app.loaded_model_size = None
                self.app.set_disabled()
                # Return the freed model memory to the OS. Without this, glibc
                # keeps the freed pages in the process heap and RSS stays at
                # the high-water mark, so the model appears to stay in memory.
                _flush_memory()
                _log_rss("after model unload")
            self._poll()

    def _on_delete_model(self, _btn):
        """Delete the selected model, or cancel its in-progress download.

        When the SELECTED model is the one being downloaded, this button reads
        "Cancel" and cancels that download. Otherwise it deletes the selected
        model after confirmation.
        """
        size = self._selected_size()
        downloading_this = (
            self.app._download_progress["active"]
            and self.app._download_progress.get("size") == size
        )
        if downloading_this:
            self.app._cancel_download()
            return
        if size not in whisper.downloaded_model_sizes():
            notify("EchoTray", f"The '{size}' model is not downloaded.", "audio-input-microphone")
            return
        # Confirm before deleting (destructive, frees disk space).
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Delete the '{size}' model?",
        )
        dialog.format_secondary_text(
            "This removes the downloaded model files from disk. You can "
            "download it again later."
        )
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.YES:
            return
        # If the model being deleted is the one currently loaded, unload it and
        # drop back to the disabled state.
        if size == self.app.loaded_model_size:
            whisper.unload_model(self.app.model)
            self.app.model = None
            self.app.loaded_model_size = None
            self.app.set_disabled()
            _flush_memory()
            _log_rss("after model unload")
        whisper.delete_model(size)
        notify("EchoTray", f"Deleted the '{size}' model.", "audio-input-microphone")
        self._poll()

    def _on_download(self, _btn):
        size = self._selected_size()
        # If the selected size is already loaded, the button is a no-op.
        if size == self.app.loaded_model_size:
            return
        # If the selected size is downloaded but not loaded, load it (no re-download).
        if size in whisper.downloaded_model_sizes():
            self.app._start_model_load(size)
        else:
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
        downloading = self.app._download_progress["active"]
        size = self._selected_size()
        downloaded = whisper.downloaded_model_sizes()
        is_downloaded = size in downloaded
        # Only show the "Downloading..."/"Cancel" state when the SELECTED model
        # is the one being downloaded. If a different model is downloading, the
        # buttons reflect the selected model's own state (Download/Delete).
        downloading_this = downloading and self.app._download_progress.get("size") == size

        # The progress bar always reflects the in-flight download (if any),
        # regardless of which model is selected.
        if downloading:
            frac = self.app._download_progress["fraction"]
            self.progress.set_fraction(frac)
            self.progress.set_text(f"{int(frac * 100)}%")
        else:
            self.progress.set_fraction(0)
            self.progress.set_text("")

        if downloading_this:
            self.download_btn.set_sensitive(False)
            self.download_btn.set_label("Downloading...")
            # The Delete button becomes a Cancel button during a download.
            self.delete_btn.set_label("Cancel")
            self.delete_btn.set_sensitive(True)
        else:
            # Download button always reads "Download"; it greys out once the
            # selected size is already downloaded.
            self.download_btn.set_label("Download")
            self.download_btn.set_sensitive(not is_downloaded)
            # Delete button greys out when the selected size isn't downloaded.
            self.delete_btn.set_label("Delete")
            self.delete_btn.set_sensitive(is_downloaded)

        # The green light, "Loaded: <model>" label, and load toggle all reflect
        # whether a model is actually LOADED into memory (self.app.model), NOT
        # merely downloaded. A model can be downloaded but still loading (or
        # failed to load), in which case the tray icon is grey and these
        # indicators must match that, not show green.
        loaded = self.app.model is not None
        self.model_light.set_ok(loaded)
        if loaded:
            self.model_label.set_text(f"Loaded: {self.app.loaded_model_size or '?'}")
        else:
            self.model_label.set_text("Select model")
        # The toggle loads/unloads the selected size. It's greyed out when the
        # selected size isn't downloaded AND nothing is loaded (nothing to load,
        # nothing to unload). When a model is loaded it stays active so the user
        # can always unload it.
        self.load_toggle.set_sensitive(is_downloaded or loaded)
        # Sync the toggle to the loaded state without re-triggering the handler.
        if self.load_toggle.get_active() != loaded:
            self.load_toggle.handler_block_by_func(self._on_load_toggle)
            self.load_toggle.set_active(loaded)
            self.load_toggle.handler_unblock_by_func(self._on_load_toggle)

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

def _clipboard_tool():
    """Return the (tool, args) to copy text to the clipboard for this session.

    Wayland uses wl-copy; X11 uses xclip (or xsel). Pick based on the display
    environment so the same build works on both.
    """
    if os.environ.get("WAYLAND_DISPLAY"):
        return ["wl-copy", "--"]
    if os.environ.get("DISPLAY"):
        # xclip is preferred; xsel is a fallback. Both take text on stdin.
        return ["xclip", "-selection", "clipboard"]
    # No display server detected - default to wl-copy and let the error path
    # report it clearly.
    return ["wl-copy", "--"]


def paste_text(text):
    """Copy text to the clipboard, then ask the helper to inject Ctrl+V."""
    tool = _clipboard_tool()
    try:
        # Step 1: Copy to the clipboard (wl-copy on Wayland, xclip on X11).
        subprocess.run(tool, input=text.encode("utf-8"), check=True, timeout=5)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[ERROR] {tool[0]}: {e}")
        body = f"{tool[0]} failed: {e}" if NOTIFY_VERBOSE else "Clipboard copy failed - check terminal for details."
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
    def _reclaim():
        """Collect Python garbage and release freed pages back to the OS."""
        _flush_memory()
        _log_rss("after flush")

    # Reject recordings that are too short to contain speech
    if len(audio_data) < whisper.SAMPLE_RATE // 10:
        if NOTIFY_ON_SKIPPED:
            notify("Skipped", "Recording too short")
        audio_data[:] = 0
        del audio_data
        _reclaim()
        GLib.idle_add(app.set_idle)
        return

    start = time.monotonic()
    try:
        text = whisper.transcribe_audio(model, audio_data)
    except Exception as e:
        print(f"[ERROR] Transcription: {e}")
        body = f"Transcription failed: {e}" if NOTIFY_VERBOSE else "Transcription failed - check terminal for details."
        notify("Error", body, urgency="critical")
        audio_data[:] = 0
        del audio_data
        _reclaim()
        GLib.idle_add(app.set_idle)
        return

    elapsed = time.monotonic() - start

    if not text:
        if NOTIFY_ON_SKIPPED:
            notify("Skipped", f"No speech detected ({elapsed:.1f}s)")
        audio_data[:] = 0
        del audio_data
        _reclaim()
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
    _reclaim()
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
        # App-owned model download state, polled by the setup window. The
        # download is owned by the app (not the window) so it continues even if
        # the window closes.
        self.model_size = whisper.MODEL_SIZE
        self.loaded_model_size = None  # size of the model currently in self.model
        self._download_thread = None
        self._load_thread = None
        self._download_cancel = threading.Event()
        self._download_result = {"ok": False, "error": None}
        self._download_progress = {"active": False, "fraction": 0.0, "bytes": 0, "size": None}
        self._load_progress = {"active": False}

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
        self._download_cancel = threading.Event()

        def _job():
            try:
                def cb(fraction, bytes_done):
                    self._download_progress["fraction"] = fraction
                    self._download_progress["bytes"] = bytes_done
                whisper.download_model(size, progress_cb=cb, cancel_event=self._download_cancel)
                self._download_result["ok"] = True
            except whisper.DownloadCancelled:
                self._download_result["error"] = "cancelled"
            except Exception as e:  # noqa: BLE001
                self._download_result["error"] = e
            finally:
                self._download_progress["active"] = False
                self._download_progress["fraction"] = 1.0
                if self._download_result["ok"]:
                    # Only auto-load the freshly downloaded model if no other
                    # model is currently loaded. If one is already loaded, leave
                    # it in place and just cache the new model for later (the
                    # user can switch to it via the toggle when ready). This
                    # lets the user download a different size without it
                    # yanking the model they're actively using out from under
                    # them.
                    if self.model is not None:
                        print(f"Model '{size}' downloaded; keeping current model loaded.")
                        return
                    try:
                        model = whisper.load_model(size)
                    except Exception as e:  # noqa: BLE001
                        print(f"[ERROR] Model load failed: {e}")
                        GLib.idle_add(notify, "EchoTray", f"Model load failed: {e}", "audio-input-microphone", "critical")
                        return
                    GLib.idle_add(self._activate_model, model, size)
                elif self._download_result["error"] == "cancelled":
                    print(f"Model '{size}' download cancelled.")
                else:
                    err = self._download_result["error"]
                    print(f"[ERROR] Model download failed: {err}")
                    GLib.idle_add(notify, "EchoTray", f"Model download failed: {err}", "audio-input-microphone", "critical")

        self._download_thread = threading.Thread(target=_job, daemon=True)
        self._download_thread.start()

    def _cancel_download(self):
        """Cancel the in-progress model download (if any)."""
        if self._download_thread is not None and self._download_thread.is_alive():
            self._download_cancel.set()

    def _start_model_load(self, size):
        """Load an already-downloaded model in the background (no re-download).

        Used when the user picks a size that is cached but not currently loaded.
        Runs on its own thread (separate from downloads) so the user can switch
        models even while a download is in progress.
        """
        if self._load_thread is not None and self._load_thread.is_alive():
            return  # a load is already in flight
        self.model_size = size
        _write_env("MODEL_SIZE", size)
        whisper.MODEL_SIZE = size
        self._load_progress = {"active": True}

        def _job():
            try:
                # Unload any previous model first so two models aren't resident.
                whisper.unload_model(self.model)
                model = whisper.load_model(size)
            except Exception as e:  # noqa: BLE001
                print(f"[ERROR] Model load failed: {e}")
                GLib.idle_add(notify, "EchoTray", f"Model load failed: {e}", "audio-input-microphone", "critical")
                return
            finally:
                self._load_progress["active"] = False
            GLib.idle_add(self._activate_model, model, size)

        self._load_thread = threading.Thread(target=_job, daemon=True)
        self._load_thread.start()

    def _activate_model(self, model, size):
        """Set the loaded model on the app and flip it to ready (main thread)."""
        self.model = model
        self.loaded_model_size = size
        self.set_ready()
        _flush_memory()
        _log_rss("after model load")
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
        print(f"[STATE] idle (icon={ICON_IDLE})")
        # Settle on green after a short delay. A no-speech stop fires amber ->
        # green in ~0ms, and the GNOME AppIndicator extension debounces icon
        # changes over a 30ms window, collapsing the rapid transition and
        # leaving the tray stuck on amber. Delaying the green set until after
        # that window makes it a separate, genuine change the extension can't
        # drop.
        GLib.timeout_add(150, self._settle_idle_icon)
        self.status_item.set_label("Status: Idle")
        try:
            self.toggle_item.set_label("Start recording")
        except Exception:
            pass

    def _settle_idle_icon(self):
        # Only settle on green if we're still idle (a new recording may have
        # started in the meantime, in which case its icon already won).
        if self.state == "IDLE":
            self.indicator.set_icon_full(ICON_IDLE, "Idle")
            self._last_icon = ICON_IDLE
        return False  # one-shot

    def set_recording(self):
        self.state = "RECORDING"
        print(f"[STATE] recording (icon={ICON_RECORDING})")
        self.indicator.set_icon_full(ICON_RECORDING, "Recording")
        self._last_icon = ICON_RECORDING
        self.status_item.set_label("Status: Recording...")
        try:
            self.toggle_item.set_label("Stop recording")
        except Exception:
            pass

    def set_transcribing(self):
        self.state = "TRANSCRIBING"
        print(f"[STATE] transcribing (icon={ICON_PROCESSING})")
        self.indicator.set_icon_full(ICON_PROCESSING, "Transcribing")
        self._last_icon = ICON_PROCESSING
        self.status_item.set_label("Status: Transcribing...")

    def _on_about(self, _widget):
        # Cache the window so repeated clicks reuse it instead of building a
        # new one each time (and leaking the old one).
        if self._about_dialog is not None:
            self._about_dialog.present()
            return
        win = AboutWindow(self)
        self._about_dialog = win
        win.connect("destroy", self._on_about_response)
        win.show_all()
        win.present()

    def _on_about_response(self, dialog, _response=None):
        # Clear the cached reference so the next About click builds a fresh
        # window (a destroyed widget can't be re-presented).
        if self._about_dialog is dialog:
            self._about_dialog = None

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
        # The "Ready" notification now fires from _activate_model() once the
        # model is actually loaded (not here), so the tray can start disabled.
        print("\nEchoTray running (tray icon visible).\n")
        self.loop.run()

    def _update_icon(self):
        """Re-assert the tray icon from the current state (poll timer).

        Only calls set_icon_full() when the icon actually changes: set_icon_full
        reloads the icon from disk on every call, so calling it every 500ms
        leaks memory continuously (the app's RSS drifts up while idle).
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
        if want is not None and want != self._last_icon:
            print(f"[ICON] poll re-assert: state={self.state} want={want}")
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

    # Apply the control styling before any window is created.
    _apply_theme()

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
        # Record the size we're actually loading so the setup window's button
        # can report "Ready" for the correct model.
        app.model_size = size
        print(f"Model '{size}' is cached - loading in the background...")
        def _load():
            try:
                model = whisper.load_model(size)
            except Exception as e:
                print(f"[ERROR] Model load failed: {e}")
                GLib.idle_add(notify, "EchoTray", f"Model load failed: {e}", "audio-input-microphone", "critical")
                return
            GLib.idle_add(app._activate_model, model, size)
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
