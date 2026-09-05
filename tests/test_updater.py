"""Offline unit tests for echotray's update-checking logic in ``src/updater.py``.

The updater module is GTK-free (imports only stdlib + version.py), so these
tests run with just the stdlib + pytest. No network, no display required.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import updater  # noqa: E402


# ── version parsing / comparison ─────────────────────────────────────────────

def test_parse_version_plain():
    assert updater.parse_version("5.4.2") == (5, 4, 2)


def test_parse_version_v_prefix():
    assert updater.parse_version("v2.3.2") == (2, 3, 2)


def test_parse_version_v_prefix_equals_plain():
    assert updater.parse_version("v2.3.2") == updater.parse_version("2.3.2")


def test_parse_version_malformed():
    assert updater.parse_version("garbage") == ()


def test_parse_version_partial():
    # Trailing non-numeric segment is ignored.
    assert updater.parse_version("2.3.x") == (2, 3)


def test_version_newer_strictly():
    assert updater.version_newer("2.3.3", "2.3.2")
    assert updater.version_newer("2.4.0", "2.3.2")
    assert updater.version_newer("3.0.0", "2.3.2")


def test_version_newer_not_equal():
    assert not updater.version_newer("2.3.2", "2.3.2")


def test_version_newer_not_older():
    assert not updater.version_newer("2.3.1", "2.3.2")


def test_version_newer_multi_digit():
    assert updater.version_newer("2.3.10", "2.3.9")


def test_version_newer_v_prefix():
    assert updater.version_newer("v2.3.3", "2.3.2")


def test_version_newer_malformed_candidate():
    assert not updater.version_newer("garbage", "2.3.2")


# ── ignored-version gating ───────────────────────────────────────────────────

def test_latest_available_version_respects_ignored(monkeypatch, tmp_path):
    # Point the updater's .env at a temp file and stub the network check.
    monkeypatch.setattr(updater, "_DOTENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(updater, "check_for_update", lambda: "v2.3.3")

    # Nothing ignored -> the version is offered.
    assert updater.latest_available_version() == "v2.3.3"

    # Ignore that exact version -> no longer offered.
    updater.ignore_version("v2.3.3")
    assert updater.latest_available_version() is None


def test_latest_available_version_ignored_older_still_offers_newer(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_DOTENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(updater, "check_for_update", lambda: "v2.3.4")

    # Ignore an OLDER version; a newer one should still be offered.
    updater.ignore_version("v2.3.3")
    assert updater.latest_available_version() == "v2.3.4"


def test_latest_available_version_none_when_no_update(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_DOTENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(updater, "check_for_update", lambda: None)
    assert updater.latest_available_version() is None


def test_ignore_version_writes_env(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_DOTENV_PATH", tmp_path / ".env")
    updater.ignore_version("v2.3.3")
    assert updater.ignored_version() == "v2.3.3"
    # And it's actually persisted to the file.
    assert "IGNORED_VERSION=v2.3.3" in (tmp_path / ".env").read_text()


def test_read_write_env_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_DOTENV_PATH", tmp_path / ".env")
    assert updater.read_env("FOO", "default") == "default"
    updater.write_env("FOO", "bar")
    assert updater.read_env("FOO", "default") == "bar"
    # Updating an existing key replaces, not appends.
    updater.write_env("FOO", "baz")
    assert updater.read_env("FOO", "default") == "baz"
    assert (tmp_path / ".env").read_text().count("FOO=") == 1


# ── update_requires_sudo ─────────────────────────────────────────────────────

def _fake_release_response(monkeypatch, body):
    """Stub the release-notes HTTP fetch to return a release with the given body."""
    import json

    class _Resp:
        def read(self):
            return json.dumps({"body": body}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(req, timeout=10):
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)


def test_update_requires_sudo_true(monkeypatch):
    _fake_release_response(monkeypatch, "Fixes the helper daemon.\n\n[requires-sudo]\n")
    assert updater.update_requires_sudo("v5.4.5") is True


def test_update_requires_sudo_false(monkeypatch):
    _fake_release_response(monkeypatch, "Just a normal bug fix.")
    assert updater.update_requires_sudo("v5.4.5") is False


def test_update_requires_sudo_mentions_token_in_prose(monkeypatch):
    # The token appearing inside prose (e.g. documenting the feature) must NOT
    # trigger the privileged path — only a standalone marker line does.
    _fake_release_response(
        monkeypatch,
        "CLI now detects a `[requires-sudo]` release and prints a note.",
    )
    assert updater.update_requires_sudo("v5.4.5") is False


def test_update_requires_sudo_empty_body(monkeypatch):
    _fake_release_response(monkeypatch, "")
    assert updater.update_requires_sudo("v5.4.5") is False


def test_update_requires_sudo_network_error(monkeypatch):
    import urllib.error

    def _urlopen(req, timeout=10):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    # Conservatively False on failure (unprivileged path is the safe default).
    assert updater.update_requires_sudo("v5.4.5") is False


# ── wrapper_path ──────────────────────────────────────────────────────────────

def test_wrapper_path_present(monkeypatch, tmp_path):
    wrapper = tmp_path / "echotray"
    wrapper.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(updater, "_INSTALL_DIR", tmp_path)
    assert updater.wrapper_path() == str(wrapper)


def test_wrapper_path_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_INSTALL_DIR", tmp_path)
    assert updater.wrapper_path() is None


# ── restart_running_app ──────────────────────────────────────────────────────

def test_restart_no_wrapper(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_INSTALL_DIR", tmp_path)  # no echotray file
    assert updater.restart_running_app() is False


def test_restart_no_lock_file(monkeypatch, tmp_path):
    wrapper = tmp_path / "echotray"
    wrapper.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(updater, "_INSTALL_DIR", tmp_path)
    monkeypatch.setattr(updater, "LOCK_PATH", tmp_path / "echotray.lock")
    assert updater.restart_running_app() is False


def test_restart_stale_lock(monkeypatch, tmp_path):
    # A lock file pointing at a dead PID must not trigger a restart.
    wrapper = tmp_path / "echotray"
    wrapper.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(updater, "_INSTALL_DIR", tmp_path)
    lock = tmp_path / "echotray.lock"
    lock.write_text("999999999\n")  # almost certainly not a live PID
    monkeypatch.setattr(updater, "LOCK_PATH", lock)
    assert updater.restart_running_app() is False


def test_restart_live_app(monkeypatch, tmp_path):
    import subprocess as sp

    wrapper = tmp_path / "echotray"
    wrapper.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(updater, "_INSTALL_DIR", tmp_path)

    # Spawn a real child process to act as the "running app".
    child = sp.Popen(["sleep", "30"])
    lock = tmp_path / "echotray.lock"
    lock.write_text(f"{child.pid}\n")
    monkeypatch.setattr(updater, "LOCK_PATH", lock)

    # The child is `sleep`, not echotray, so stub the PID-identity check to
    # confirm it's an echotray process (we test _pid_is_echotray separately).
    monkeypatch.setattr(updater, "_pid_is_echotray", lambda pid: True)

    # Capture the relaunch so we don't actually spawn the wrapper.
    spawned = []
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or child)

    try:
        assert updater.restart_running_app() is True
        assert spawned, "should have relaunched the wrapper"
    finally:
        child.kill()
        child.wait()


def test_restart_rejects_non_echotray_pid(monkeypatch, tmp_path):
    import subprocess as sp

    wrapper = tmp_path / "echotray"
    wrapper.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(updater, "_INSTALL_DIR", tmp_path)

    # A live process whose cmdline is NOT echotray (PID reuse) must be left
    # alone — no SIGTERM, no relaunch.
    child = sp.Popen(["sleep", "30"])
    lock = tmp_path / "echotray.lock"
    lock.write_text(f"{child.pid}\n")
    monkeypatch.setattr(updater, "LOCK_PATH", lock)

    spawned = []
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or child)

    try:
        assert updater.restart_running_app() is False
        assert not spawned, "must not relaunch for a non-echotray PID"
        # The child must still be alive (we didn't signal it).
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()
