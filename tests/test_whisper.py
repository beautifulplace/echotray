"""Offline unit tests for echotray's pure model-cache / download / transcription
logic in ``src/whisper.py``.

The heavy ML / audio deps (faster-whisper, sounddevice, numpy) are imported
lazily inside whisper.py, so this whole suite runs with just the stdlib + pytest
(numpy is only needed for the two AudioRecorder.stop tests, which skip when it
isn't installed). No network, no audio device, no GPU is required.
"""

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import whisper  # noqa: E402

# Env keys whisper reads at import time — used to force a clean default-isolated
# module between tests.
_CONFIG_ENV_KEYS = (
    "SAMPLE_RATE",
    "CHANNELS",
    "MAX_RECORDING_SECONDS",
    "CPU_THREADS",
    "MODEL_SIZE",
    "WHISPER_LANGUAGE",
    "COMPUTE_TYPE",
    "MODEL_DIR",
)


@pytest.fixture(autouse=True)
def _reset_whisper(monkeypatch):
    """After every test, drop config env vars and reload whisper so each test
    starts from a default-config module (no cross-test pollution)."""
    yield
    for key in _CONFIG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    importlib.reload(whisper)


# ── env parsing (import-time) ────────────────────────────────────────────────

def _reload_with_env(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return importlib.reload(whisper)


def test_defaults_used_when_env_absent(monkeypatch):
    m = _reload_with_env(monkeypatch)
    assert m.SAMPLE_RATE == 16000
    assert m.CHANNELS == 1
    assert m.MAX_RECORDING_SECONDS == 300
    assert m.CPU_THREADS == 4
    assert m.MODEL_SIZE == "small"
    assert m.LANGUAGE == "en"


def test_env_values_are_parsed(monkeypatch):
    m = _reload_with_env(
        monkeypatch,
        SAMPLE_RATE=48000,
        CHANNELS=2,
        MAX_RECORDING_SECONDS=60,
        CPU_THREADS=2,
        MODEL_SIZE="large-v3",
        WHISPER_LANGUAGE="",
    )
    assert m.SAMPLE_RATE == 48000
    assert m.CHANNELS == 2
    assert m.MAX_RECORDING_SECONDS == 60
    assert m.CPU_THREADS == 2
    assert m.MODEL_SIZE == "large-v3"
    assert m.LANGUAGE is None  # empty string → auto-detect


def test_invalid_env_value_does_not_crash_and_falls_back(monkeypatch, capsys):
    # The regression this guards against: a non-numeric .env value used to raise
    # ValueError at import and brick the whole GUI with a traceback.
    m = _reload_with_env(monkeypatch, SAMPLE_RATE="sixteen-thousand")
    assert m.SAMPLE_RATE == 16000
    assert "[config]" in capsys.readouterr().err


def test_env_value_is_clamped(monkeypatch, capsys):
    m = _reload_with_env(monkeypatch, CPU_THREADS=0, CHANNELS=9)
    assert m.CPU_THREADS == 1  # clamped to lo=1
    assert m.CHANNELS == 2  # clamped to hi=2
    err = capsys.readouterr().err
    assert "clamping" in err


def test_import_does_not_pull_heavy_ml_deps(capsys):
    """Guard for the lazy-import refactor: importing whisper must stay stdlib-only."""
    for mod in ("numpy", "sounddevice", "faster_whisper"):
        assert mod not in sys.modules, f"{mod} was imported at module level"


# ── _env_int helper ──────────────────────────────────────────────────────────

def test_env_int_default_when_unset(monkeypatch):
    monkeypatch.delenv("ET_TEST_INT", raising=False)
    assert whisper._env_int("ET_TEST_INT", 42) == 42


def test_env_int_parses_valid(monkeypatch):
    monkeypatch.setenv("ET_TEST_INT", "7")
    assert whisper._env_int("ET_TEST_INT", 42) == 7


def test_env_int_invalid_falls_back(monkeypatch, capsys):
    monkeypatch.setenv("ET_TEST_INT", "not-a-number")
    assert whisper._env_int("ET_TEST_INT", 42) == 42
    assert "[config]" in capsys.readouterr().err


def test_env_int_empty_string_falls_back(monkeypatch):
    monkeypatch.setenv("ET_TEST_INT", "   ")
    assert whisper._env_int("ET_TEST_INT", 42) == 42


def test_env_int_clamps_bounds(monkeypatch):
    monkeypatch.setenv("ET_TEST_INT", "1")
    assert whisper._env_int("ET_TEST_INT", 42, lo=5, hi=10) == 5
    monkeypatch.setenv("ET_TEST_INT", "99")
    assert whisper._env_int("ET_TEST_INT", 42, lo=5, hi=10) == 10


# ── model file layout ────────────────────────────────────────────────────────

def test_model_files_standard_vs_large_v3():
    assert whisper._model_files("small") == [
        "config.json",
        "tokenizer.json",
        "vocabulary.txt",
        "model.bin",
    ]
    assert whisper._model_files("large-v3") == [
        "config.json",
        "tokenizer.json",
        "vocabulary.json",
        "preprocessor_config.json",
        "model.bin",
    ]
    # unknown sizes use the standard file set
    assert whisper._model_files("bogus") == whisper._model_files("tiny")


def test_model_local_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    assert whisper._model_local_dir("small") == os.path.join(str(tmp_path), "small")


# ── cache completeness ───────────────────────────────────────────────────────

def _write_files(root, size, names):
    d = os.path.join(root, size)
    os.makedirs(d, exist_ok=True)
    for name in names:
        with open(os.path.join(d, name), "w") as f:
            f.write("x")
    return d


def test_model_complete_true_when_all_files_present(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    _write_files(tmp_path, "small", whisper._model_files("small"))
    assert whisper._model_complete("small") is True
    assert whisper.model_needs_download("small") is False


def test_model_complete_false_on_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    assert whisper._model_complete("small") is False
    assert whisper.model_needs_download("small") is True


def test_model_complete_false_on_empty_file(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    d = _write_files(tmp_path, "small", whisper._model_files("small"))
    with open(os.path.join(d, "model.bin"), "w") as f:
        f.write("")  # empty → incomplete
    assert whisper._model_complete("small") is False


def test_model_complete_false_on_missing_one_file(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    present = whisper._model_files("small")[:-1]
    _write_files(tmp_path, "small", present)
    assert whisper._model_complete("small") is False


def test_downloaded_model_sizes_lists_only_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    _write_files(tmp_path, "small", whisper._model_files("small"))
    _write_files(tmp_path, "base", whisper._model_files("base")[:-1])  # incomplete
    # a bare dir that isn't a valid size contributes nothing
    _write_files(tmp_path, "junk", whisper._model_files("tiny")[:-2])
    found = whisper.downloaded_model_sizes()
    assert found == ["small"]


def test_downloaded_model_sizes_empty_cache_root(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    assert whisper.downloaded_model_sizes() == []


# ── delete_model ─────────────────────────────────────────────────────────────

def test_delete_model_removes_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    d = _write_files(tmp_path, "medium", whisper._model_files("medium"))
    assert os.path.isdir(d)
    assert whisper.delete_model("medium") is True
    assert not os.path.isdir(d)


def test_delete_model_missing_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    assert whisper.delete_model("never-downloaded") is False


# ── _download_file ───────────────────────────────────────────────────────────

class _FakeResp:
    """Mimics urllib's HTTPResponse: .headers, .read(), context manager."""

    def __init__(self, data, content_length=None, method_hint=None):
        self._data = data
        self._pos = 0
        self.headers = {"Content-Length": str(content_length if content_length is not None else len(data))}
        self.method_hint = method_hint

    def read(self, n=None):
        if self._pos >= len(self._data):
            return b""
        segment = self._data[self._pos : self._pos + n]
        self._pos += len(segment)
        return segment

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_urlopen(monkeypatch, responses):
    """Replace urllib.request.urlopen with a callable serving canned responses.

    `responses` maps a method ('HEAD'/'GET') to a _FakeResp (or a factory). If a
    dict is given, the METHOD is looked up; the same response is returned each call.
    """
    import urllib.request

    recorded = []
    fake = _FakeResp  # local alias

    def urlopen(req, timeout=30):
        recorded.append((getattr(req, "method", None) or "GET", req.full_url))
        key = getattr(req, "method", None) or "GET"
        resp = responses[key]
        return resp if not callable(resp) else resp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return recorded


def test_download_file_writes_and_reports_progress(monkeypatch):
    data = b"hello world" * 40  # ensure multiple chunks vs small chunk_size
    recorded = _install_fake_urlopen(monkeypatch, {"GET": _FakeResp(data)})
    calls = []
    dest = "/tmp/et_progress.bin"
    if os.path.exists(dest):
        os.remove(dest)
    try:
        result = whisper._download_file("https://huggingface.co/x/config.json", dest, progress_cb=lambda frac, done: calls.append((frac, done)), chunk_size=16)
        assert result == dest
        with open(dest, "rb") as f:
            assert f.read() == data
        assert len(calls) >= 2
        fracs = [c[0] for c in calls]
        assert fracs[-1] == 1.0  # final fraction is complete
        assert recorded == [("GET", "https://huggingface.co/x/config.json")]
    finally:
        if os.path.exists(dest):
            os.remove(dest)


def test_download_file_cancel_raises_and_stops(monkeypatch):
    import threading

    data = b"0123456789abcdef" * 100  # 1600 bytes
    # Set the cancel event after the first chunk is read/written.
    cancel = threading.Event()
    reads = {"n": 0}

    orig_resp = _FakeResp(data)

    def factory():
        return orig_resp

    recorded = _install_fake_urlopen(monkeypatch, {"GET": factory})

    def cancelling_cb(frac, done):
        reads["n"] += 1
        if reads["n"] >= 1:
            cancel.set()

    dest = "/tmp/et_cancel.bin"
    if os.path.exists(dest):
        os.remove(dest)
    try:
        with pytest.raises(whisper.DownloadCancelled):
            whisper._download_file("https://huggingface.co/x/model.bin", dest, progress_cb=cancelling_cb, chunk_size=16, cancel_event=cancel)
        # Partial file may exist (written before the cancel), but we must NOT
        # have written all the data.
        bytes_written = os.path.getsize(dest) if os.path.exists(dest) else 0
        assert bytes_written < len(data)
    finally:
        if os.path.exists(dest):
            os.remove(dest)


# ── download_model ───────────────────────────────────────────────────────────

def _nice_urlopen_for_download(monkeypatch, files):
    """Serve HEAD + GET responses for a {name: bytes} dict."""
    import urllib.request

    requested = []

    def urlopen(req, timeout=30):
        name = req.full_url.rstrip("/").split("/")[-1]
        requested.append(name)
        method = getattr(req, "method", None) or "GET"
        body = files[name]
        if method == "HEAD":
            return _FakeResp(b"", content_length=len(body))
        return _FakeResp(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return requested


def test_download_model_fetches_missing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    files = {name: name.encode() for name in whisper._model_files("small")}
    requested = _nice_urlopen_for_download(monkeypatch, files)
    d = whisper.download_model("small")
    assert d == os.path.join(str(tmp_path), "small")
    for name, body in files.items():
        with open(os.path.join(d, name), "rb") as f:
            assert f.read() == body
    # every required file was requested
    assert set(requested) == set(whisper._model_files("small"))
    # cache is now complete
    assert whisper.model_needs_download("small") is False


def test_download_model_skips_already_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    files = {name: name.encode() for name in whisper._model_files("small")}
    d = whisper._model_local_dir("small")
    os.makedirs(d, exist_ok=True)
    for name, body in files.items():
        with open(os.path.join(d, name), "wb") as f:
            f.write(body)
    # No network requests should happen at all if everything is cached.
    requested = _nice_urlopen_for_download(monkeypatch, files)
    assert whisper.download_model("small") == d
    assert requested == []


def test_download_model_returns_early_when_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    _write_files(tmp_path, "small", whisper._model_files("small"))
    # urlopen would fail if called — cached path must not touch the network.
    import urllib.request

    def boom(*a, **k):
        raise AssertionError("network should not be hit")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    d = whisper.download_model("small")
    assert d == os.path.join(str(tmp_path), "small")


def test_download_model_cancel_cleans_partial_files(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    import threading

    big = b"x" * (2 * 1024 * 1024 + 100)  # > 2 chunks on a 1 MiB chunk_size
    files = {name: big for name in whisper._model_files("base")}
    cancel = threading.Event()

    _nice_urlopen_for_download(monkeypatch, files)

    # Only the FIRST file gets downloaded; we cancel it mid-download.
    names = whisper._model_files("base")
    first_dest = os.path.join(str(tmp_path), "base", names[0])

    count = 0
    done_bytes_holder = {"n": 0}

    def cancelling_cb(frac, done):
        nonlocal count
        count += 1
        done_bytes_holder["n"] = done
        if count >= 2:
            cancel.set()

    with pytest.raises(whisper.DownloadCancelled):
        whisper.download_model("base", progress_cb=cancelling_cb, cancel_event=cancel)

    # The partially-downloaded dest must NOT exist, and no .part must linger.
    assert not os.path.exists(first_dest)
    assert not os.path.exists(first_dest + ".part"), "stale .part should be cleaned up"
    # No other files should have been downloaded either.
    leftover = [n for n in names if n != names[0] and os.path.exists(os.path.join(os.path.dirname(first_dest), n))]
    assert leftover == []


# ── load_model / unload_model ────────────────────────────────────────────────

class _RecorderWhisper:
    """Registry that records every construction call to faster-whisper's model class."""

    calls = []

    def __init__(self, *args, **kwargs):
        type(self).calls.append((args, kwargs))


class _FakeFasterWhisper:
    WhisperModel = _RecorderWhisper


@pytest.fixture
def fake_fw(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", _FakeFasterWhisper())
    _RecorderWhisper.calls.clear()
    yield
    sys.modules.pop("faster_whisper", None)


def test_load_model_uses_local_dir_when_cached(tmp_path, monkeypatch, fake_fw):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    _write_files(tmp_path, "small", whisper._model_files("small"))
    model = whisper.load_model("small")
    assert model is not None
    assert len(_RecorderWhisper.calls) == 1
    args, kwargs = _RecorderWhisper.calls[0]
    assert args[0] == os.path.join(str(tmp_path), "small")  # local dir, not the size name
    assert kwargs["device"] == "cpu"
    assert kwargs["compute_type"] == "int8"
    assert kwargs["cpu_threads"] == whisper.CPU_THREADS


def test_load_model_uses_size_name_when_not_cached(tmp_path, monkeypatch, fake_fw, capsys):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    model = whisper.load_model("base")
    args, _kwargs = _RecorderWhisper.calls[0]
    assert args[0] == "base"  # let faster-whisper pull it
    assert model is not None


def test_load_model_defaults_size_to_configured(tmp_path, monkeypatch, fake_fw):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    whisper.load_model()  # size=None → MODEL_SIZE default ("small")
    args, _kwargs = _RecorderWhisper.calls[0]
    assert args[0] == "small"


def test_load_model_coerces_float16_to_int8(tmp_path, monkeypatch, fake_fw, capsys):
    monkeypatch.setattr(whisper, "MODEL_CACHE_ROOT", str(tmp_path))
    m = _reload_with_env(monkeypatch, COMPUTE_TYPE="float16")
    m.load_model("small")
    args, kwargs = _RecorderWhisper.calls[0]
    assert kwargs["compute_type"] == "int8"
    assert "Warning" in capsys.readouterr().out


def test_unload_model_none_is_noop():
    assert whisper.unload_model(None) is None


def test_unload_model_calls_unload():
    class Inner:
        def unload_model(self):
            Inner.called = True

    Inner.called = False
    model = type("Model", (), {"model": Inner()})()
    whisper.unload_model(model)
    assert Inner.called is True


def test_unload_model_without_unload_method_is_noop():
    model = type("Model", (), {"model": type("X", (), {})()})()
    whisper.unload_model(model)  # should not raise


# ── transcribe_audio ─────────────────────────────────────────────────────────

class _Seg:
    def __init__(self, text):
        self.text = text

    @classmethod
    def transcribe(cls, audio, **kw):
        return (_Seg.segments, "info")

    segments = []


class _FakeModel:
    def __init__(self, segments):
        self._segs = segments

    def transcribe(self, audio, **kwargs):
        return self._segs, {"language": "en"}


def test_transcribe_audio_joins_segments_and_appends_space():
    model = _FakeModel([_Seg("hello"), _Seg("world")])
    assert whisper.transcribe_audio(model, b"\x00") == "hello world "


def test_transcribe_audio_strips_segment_whitespace():
    model = _FakeModel([_Seg("  spaced  "), _Seg("\nweird\n")])
    assert whisper.transcribe_audio(model, b"\x00") == "spaced weird "


def test_transcribe_audio_empty_has_no_trailing_space():
    model = _FakeModel([])
    assert whisper.transcribe_audio(model, b"\x00") == ""


# ── AudioRecorder (needs numpy for the array paths) ──────────────────────────

def test_audio_recorder_stop_empty_returns_empty_array(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    r = whisper.AudioRecorder()
    r.chunks = []
    r.stream = None
    out = r.stop()
    assert out.shape == (0,) and out.dtype == np.float32


def test_audio_recorder_stop_concatenates_and_flattens(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    r = whisper.AudioRecorder()
    r.stream = None
    r.chunks = [
        np.array([[1.0], [2.0]], dtype=np.float32),
        np.array([[3.0], [4.0]], dtype=np.float32),
    ]
    out = r.stop()
    assert list(out) == [1.0, 2.0, 3.0, 4.0]
    assert out.dtype == np.float32