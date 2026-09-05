"""Update checking and installation, shared by the GUI and the CLI.

This module is deliberately GTK-free so the `echotray upgrade` CLI can use it
without importing the GTK app (which needs a display and the GTK bindings). It
owns:

- version parsing/comparison
- the repo tag check (Gitea build -> Gitea, GitHub build -> GitHub)
- downloading + installing a tagged release
- the "ignored version" state in the app's .env

The install dir and .env path are resolved from this file's location (the app
lives at <install>/app/src/), so the CLI works from the installed copy even if
the user deleted their original clone.
"""

import os
import pathlib
import shutil
import subprocess
import sys

from version import __version__, REPO_URL

# The app lives at <install>/app/src/updater.py, so:
#   src/updater.py -> app/src/updater.py -> app/ -> install dir
_INSTALL_DIR = pathlib.Path(__file__).resolve().parent.parent.parent
_DOTENV_PATH = _INSTALL_DIR / ".env"

# Single-instance lock file (shared with app.py). The running app writes its
# PID here; the CLI reads it to detect and restart a live instance.
LOCK_PATH = pathlib.Path("/tmp/echotray.lock")


def wrapper_path():
    """The path to the installed `echotray` launcher wrapper, or None.

    install.sh writes a wrapper at <install>/echotray and symlinks it into
    ~/.local/bin. Relaunching through the wrapper (rather than re-invoking
    app.py directly) preserves the original launch context: the venv python,
    the detached session, and the log redirect. Returns None if the wrapper
    isn't present (e.g. running from a dev clone), so callers can fall back.
    """
    candidate = _INSTALL_DIR / "echotray"
    if candidate.is_file():
        return str(candidate)
    return None


def _pid_is_echotray(pid):
    """True if /proc/<pid>/cmdline looks like a running EchoTray process.

    Guards against PID reuse: the lock file may hold a stale PID that now
    belongs to an unrelated process. We only signal a process whose command
    line actually references echotray (the installed path) or app.py (a dev
    clone). Fails closed (returns False) if /proc is unavailable.
    """
    try:
        raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    text = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
    return "echotray" in text or "app.py" in text


def restart_running_app():
    """Restart a running EchoTray instance after an upgrade, if one is running.

    Reads the PID from the single-instance lock file, verifies it is actually
    an EchoTray process (not a reused PID), sends SIGTERM (which exits cleanly
    and releases the lock), waits for it to exit, then relaunches through the
    wrapper. Returns True if a restart was performed, False if no instance was
    running (or the wrapper is missing).
    """
    import signal
    import time

    wrapper = wrapper_path()
    if wrapper is None:
        return False

    try:
        pid = int(LOCK_PATH.read_text().strip())
    except (OSError, ValueError):
        return False

    # Is that PID alive AND actually an EchoTray process? (A stale lock file,
    # or a reused PID, must not cause us to signal an unrelated process.)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if not _pid_is_echotray(pid):
        return False

    # Ask it to exit, then wait for it to release the lock (the kernel drops
    # the flock when the process exits).
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(50):  # up to ~5s
        try:
            os.kill(pid, 0)
        except OSError:
            break  # process gone
        time.sleep(0.1)

    # Relaunch through the wrapper (detached, like a normal launch).
    subprocess.Popen(
        [wrapper],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True,
    )
    return True


# ── version parsing ──────────────────────────────────────────────────────────

def parse_version(v):
    """Parse a version string like '5.4.2' or 'v2.3.2' into a tuple of ints.

    Trailing non-numeric segments are ignored, so '2.3.2' and 'v2.3.2' both
    parse to (2, 3, 2). A malformed version parses to an empty tuple.
    """
    v = (v or "").strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts)


def version_newer(candidate, current):
    """True if `candidate` is a strictly newer version than `current`."""
    return parse_version(candidate) > parse_version(current)


# ── repo / URL helpers ───────────────────────────────────────────────────────

def _repo_parts():
    """Return (host, owner, repo) parsed from REPO_URL."""
    from urllib.parse import urlparse
    u = urlparse(REPO_URL)
    parts = [p for p in u.path.split("/") if p]
    owner = parts[0] if len(parts) > 0 else ""
    repo = parts[1] if len(parts) > 1 else ""
    return u.netloc, owner, repo


def _update_check_url():
    """The API endpoint listing tags, matching this build's forge."""
    host, owner, repo = _repo_parts()
    if "github.com" in host:
        return f"https://api.github.com/repos/{owner}/{repo}/tags"
    return f"https://{host}/api/v1/repos/{owner}/{repo}/tags"


def _update_tarball_url(version):
    """The source tarball URL for a given version tag.

    `version` is the raw tag name (e.g. "v2.3.2"), which already carries the
    "v" prefix, so it's used verbatim in the URL.
    """
    host, owner, repo = _repo_parts()
    if "github.com" in host:
        return f"https://github.com/{owner}/{repo}/archive/refs/tags/{version}.tar.gz"
    return f"https://{host}/{owner}/{repo}/archive/{version}.tar.gz"


# ── .env state (ignored version) ─────────────────────────────────────────────

def read_env(key, default=""):
    """Read a single value from the app's .env file (empty string if unset)."""
    try:
        lines = _DOTENV_PATH.read_text().splitlines() if _DOTENV_PATH.exists() else []
    except OSError:
        return default
    for line in lines:
        if line.strip().startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return default


def write_env(key, value):
    """Set `key=value` in the app's .env file (creating/updating the line)."""
    try:
        lines = _DOTENV_PATH.read_text().splitlines() if _DOTENV_PATH.exists() else []
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
        _DOTENV_PATH.write_text("\n".join(lines) + "\n")
    except OSError:
        pass


def ignored_version():
    """The version the user chose to ignore, or "" if none."""
    return read_env("IGNORED_VERSION", "").strip()


def ignore_version(version):
    """Record `version` as ignored (hides the update pill until a newer one)."""
    write_env("IGNORED_VERSION", version)


# ── update check ─────────────────────────────────────────────────────────────

def check_for_update():
    """Return the latest available version string, or None.

    Queries the repo's tag list (GitHub or Gitea, matching this build) and
    returns the newest tag strictly newer than __version__. Returns None on any
    error (no network, no tags, malformed response), logging the reason to
    stderr so a silent failure is still diagnosable.
    """
    import json
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(
            _update_check_url(),
            headers={"User-Agent": "echotray", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"[update] check failed (network): {e}", file=sys.stderr)
        return None
    except (ValueError, OSError) as e:
        print(f"[update] check failed: {e}", file=sys.stderr)
        return None
    if not isinstance(tags, list):
        print("[update] check failed: unexpected response (not a list)", file=sys.stderr)
        return None
    latest = None
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        name = tag.get("name", "")
        if version_newer(name, __version__):
            if latest is None or version_newer(name, latest):
                latest = name
    return latest


def latest_available_version():
    """The newest version to offer, or None if none (or the latest is ignored).

    Respects the ignored version: if the newest tag is not strictly newer than
    the ignored version, returns None (nothing to show).
    """
    latest = check_for_update()
    if latest is None:
        return None
    ignored = ignored_version()
    if ignored and not version_newer(latest, ignored):
        return None
    return latest


def update_requires_sudo(version):
    """Return True if the release for `version` needs a privileged install.

    A release is marked privileged by a line containing exactly the token
    `[requires-sudo]` (on its own line) in its release notes. The maintainer
    sets this when a release changes the helper daemon or adds system packages
    (the two things that need root). The token is matched as a standalone line
    so it can't be triggered by prose that merely mentions the marker. If the
    release notes can't be read, we conservatively return False (the
    unprivileged path is the safe default).
    """
    import json
    import urllib.error
    import urllib.request

    host, owner, repo = _repo_parts()
    if "github.com" in host:
        url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{version}"
    else:
        url = f"https://{host}/api/v1/repos/{owner}/{repo}/releases/tags/{version}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "echotray", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            rel = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError) as e:
        print(f"[update] could not read release notes for {version}: {e}", file=sys.stderr)
        return False
    body = rel.get("body") or ""
    for line in body.splitlines():
        if line.strip() == "[requires-sudo]":
            return True
    return False


# ── install ──────────────────────────────────────────────────────────────────

def download_and_install(version, skip_root=True):
    """Download the tagged source tarball and run install.sh to apply it.

    Returns the path to the extracted source root (so the caller can run
    install.sh itself, e.g. with sudo), or raises on failure. When
    `skip_root` is True, install.sh is run unprivileged with ECHOTRAY_SKIP_ROOT=1
    (skips apt + helper daemon, which are already installed); when False, the
    caller is expected to run install.sh themselves (e.g. `sudo bash install.sh`).
    """
    import tempfile
    import urllib.request

    url = _update_tarball_url(version)
    tmpdir = tempfile.mkdtemp(prefix="echotray-update-")
    tarball = os.path.join(tmpdir, "echotray.tar.gz")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "echotray"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(tarball, "wb") as f:
            shutil.copyfileobj(resp, f)

        extract_dir = os.path.join(tmpdir, "src")
        os.makedirs(extract_dir, exist_ok=True)
        shutil.unpack_archive(tarball, extract_dir)

        # The tarball extracts to a single top-level dir (e.g. echotray-<sha>).
        entries = [e for e in os.listdir(extract_dir)
                   if os.path.isdir(os.path.join(extract_dir, e))]
        if not entries:
            raise RuntimeError("downloaded archive was empty")
        src_root = os.path.join(extract_dir, entries[0])

        install_sh = os.path.join(src_root, "install.sh")
        if not os.path.isfile(install_sh):
            raise RuntimeError("install.sh not found in the downloaded archive")

        if skip_root:
            env = dict(os.environ, ECHOTRAY_SKIP_ROOT="1")
            subprocess.run(["bash", install_sh], check=True, env=env)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None
        # Caller runs install.sh (with sudo); keep the extracted tree around.
        return src_root
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
