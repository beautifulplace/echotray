"""Command-line interface for EchoTray maintenance commands.

Runs from the installed copy (not the original clone), so it works even after
the user deletes the source they pulled. GTK-free: it imports only `updater`
and `version`, so it needs no display or GTK bindings.

Usage:
    echotray upgrade [--sudo]   check for a newer version and install it
    echotray ignore <version>   hide the update pill for a specific version
    echotray check              print the latest available version (if any)
"""

import os
import shutil
import subprocess
import sys

import updater
from version import __version__


def _print_usage():
    print("EchoTray " + __version__)
    print()
    print("Usage:")
    print("  echotray upgrade [--sudo]   install the latest version")
    print("  echotray ignore <version>   hide the update pill for a version")
    print("  echotray check              show the latest available version")
    print()


def cmd_check():
    latest = updater.latest_available_version()
    if latest is None:
        print("EchoTray is up to date (" + __version__ + ").")
        return 0
    print(f"A newer version is available: {latest} (you have {__version__}).")
    return 0


def cmd_ignore(version):
    if not version:
        print("Usage: echotray ignore <version>", file=sys.stderr)
        return 2
    updater.ignore_version(version)
    print(f"Ignoring version {version}. The update pill will stay hidden until a newer version appears.")
    return 0


def cmd_upgrade(use_sudo):
    latest = updater.latest_available_version()
    if latest is None:
        print("EchoTray is up to date (" + __version__ + ").")
        return 0

    requires_sudo = updater.update_requires_sudo(latest)

    # If the release needs a privileged install and the user didn't pass
    # --sudo, tell them up front (before touching anything) and stop.
    if requires_sudo and not use_sudo:
        print(f"EchoTray {latest} requires a privileged install (it changes the "
              "helper daemon or system packages).")
        print("Run this instead:")
        print("  echotray upgrade --sudo")
        return 1

    print(f"Upgrading EchoTray {__version__} -> {latest} ...")
    try:
        if use_sudo:
            # Full install: download + extract, then run install.sh with sudo so
            # the helper daemon and any new system packages are applied too.
            src_root = updater.download_and_install(latest, skip_root=False)
            if not src_root:
                print("Upgrade failed: could not download the release.", file=sys.stderr)
                return 1
            install_sh = os.path.join(src_root, "install.sh")
            print("Running install.sh (sudo will prompt for your password)...")
            try:
                subprocess.run(["sudo", "bash", install_sh], check=True)
            finally:
                # src_root is <tmpdir>/src/<topdir>; remove the whole temp tree.
                shutil.rmtree(os.path.dirname(os.path.dirname(src_root)), ignore_errors=True)
        else:
            # Unprivileged install: skips the root-only steps (apt + helper
            # daemon), which are already installed (we already confirmed this
            # release doesn't need them).
            updater.download_and_install(latest, skip_root=True)
    except subprocess.CalledProcessError as e:
        print(f"Upgrade failed: install.sh exited with an error (code {e.returncode}).",
              file=sys.stderr)
        print("Check your internet connection and try again, or run "
              "'echotray upgrade --sudo' if this release needs it.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Upgrade failed: {e}", file=sys.stderr)
        print("Check your internet connection and try again.", file=sys.stderr)
        return 1

    print(f"Upgraded to {latest}.")
    if updater.restart_running_app():
        print("EchoTray was running and has been restarted with the new version.")
    else:
        print("Restart EchoTray to use the new version.")
    return 0


def main(argv):
    if not argv:
        _print_usage()
        return 0

    cmd = argv[0]
    if cmd == "check":
        return cmd_check()
    if cmd == "ignore":
        return cmd_ignore(argv[1] if len(argv) > 1 else "")
    if cmd == "upgrade":
        use_sudo = "--sudo" in argv[1:]
        return cmd_upgrade(use_sudo)
    if cmd in ("-h", "--help", "help"):
        _print_usage()
        return 0

    print(f"Unknown command: {cmd}", file=sys.stderr)
    _print_usage()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
