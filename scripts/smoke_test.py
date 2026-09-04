#!/usr/bin/env python3
"""Headless smoke test for EchoTray's GTK windows.

Instantiates the Setup window under a virtual display, so GTK API mistakes
(wrong method names, missing arguments) are caught before the user opens a
window. Run inside the app venv.

Usage:
    python3 smoke_test.py [APP_SRC_DIR]

APP_SRC_DIR defaults to ~/.local/share/echotray/app/src (the install dir).
Exit 0 on success; any exception prints a traceback and exits non-zero.
"""
import os
import sys

APP_SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
    "~/.local/share/echotray/app/src")
sys.path.insert(0, APP_SRC)

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import GLib, Gtk  # noqa: E402

import app as app_module  # noqa: E402


class _FakeApp:
    """Minimal stand-in for DictationApp, exposing only what the window touches."""

    def __init__(self):
        self._download_progress = {"active": False, "fraction": 0.0,
                                   "bytes": 0, "size": None}
        self.loaded_model_size = None
        self.model_size = "small"
        self._setup_window = None

    def _start_model_load(self, size):
        pass

    def _start_model_download(self, size):
        pass


def pump(ms=300):
    """Run the GTK main loop briefly so widgets realize and draw."""
    ctx = GLib.MainContext.default()
    end = GLib.get_monotonic_time() + ms * 1000
    while GLib.get_monotonic_time() < end:
        while ctx.pending():
            ctx.iteration(False)
        GLib.usleep(10000)


def main():
    # Exercise the theme provider (the X11/Wayland screen path).
    app_module._apply_theme()

    fake = _FakeApp()

    setup = app_module.SetupWindow(fake)
    setup.show_all()
    pump()
    setup._poll()  # exercise the poll path (touches app._download_progress etc.)

    # Exercise the custom About window: build it, navigate to the
    # troubleshooting page, then to the debug page (which builds the debug
    # text and the Copy/Save As buttons).
    about = app_module.AboutWindow(fake)
    about.show_all()
    pump()
    about._show_troubleshooting(None)
    pump()
    about._show_debug(None)
    pump()
    assert about._debug_text.startswith("EchoTray "), "debug text should start with app name"
    about._on_back(None)
    pump()
    about._on_back(None)
    pump()

    print("SMOKE TEST OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
