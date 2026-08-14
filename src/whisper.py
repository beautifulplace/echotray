"""Audio recording, Whisper transcription, and model download/caching.

Privileged input (injecting Ctrl+V via /dev/uinput) is handled by the root-owned
echotray-helperd daemon. This module handles microphone capture, Whisper
transcription, and the on-demand model download with progress reporting.
"""

import os
import threading
import time

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from faster_whisper import WhisperModel

load_dotenv()

# CTranslate2's WhisperModel.transcribe() is NOT safe to call concurrently on
# the same model instance. Two overlapping calls can deadlock internally
# (idle CPU, frozen app, memory held). Serialize all transcription with a lock.
_TRANSCRIBE_LOCK = threading.Lock()

# ── Configuration (from .env) ─────────────────────────────────────────────────

MODEL_SIZE = os.getenv("MODEL_SIZE", "small")
COMPUTE_TYPE = os.getenv("COMPUTE_TYPE", "int8")
LANGUAGE = os.getenv("WHISPER_LANGUAGE", "en") or None  # empty string → None = auto-detect
SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "16000"))
CHANNELS = int(os.getenv("CHANNELS", "1"))
MAX_RECORDING_SECONDS = int(os.getenv("MAX_RECORDING_SECONDS", "300"))


# ── Whisper Model ─────────────────────────────────────────────────────────────

# Mapping of faster-whisper model size -> HuggingFace repo + required files
_MODEL_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
}
_MODEL_FILES = ["config.json", "tokenizer.json", "vocabulary.txt", "model.bin"]

MODEL_CACHE_ROOT = os.getenv(
    "MODEL_DIR", os.path.join(os.path.expanduser("~"), ".local", "share", "echotray", "models")
)


def _model_local_dir(size):
    return os.path.join(MODEL_CACHE_ROOT, size)


def _download_file(url, dest, progress_cb=None, chunk_size=1 << 20):
    """Stream a file from `url` to `dest`, reporting (fraction, bytes) to progress_cb."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "echotray/3.2"})
    with urllib.request.urlopen(req) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    frac = (done / total) if total > 0 else 0.0
                    progress_cb(frac, done)
    return dest


def model_needs_download(size):
    """True if the model files aren't fully cached locally yet."""
    d = _model_local_dir(size)
    if not os.path.isdir(d):
        return True
    # model.bin is the large file; its presence implies a complete download.
    return not os.path.isfile(os.path.join(d, "model.bin"))


def downloaded_model_sizes():
    """Return the list of model sizes that are fully downloaded/cached.

    Scans the model cache root for any size whose model.bin is present, so the
    app can detect a model the user already downloaded even if it isn't the
    configured default size.
    """
    found = []
    if not os.path.isdir(MODEL_CACHE_ROOT):
        return found
    for name in os.listdir(MODEL_CACHE_ROOT):
        d = os.path.join(MODEL_CACHE_ROOT, name)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, "model.bin")):
            found.append(name)
    return found


def download_model(size, progress_cb=None):
    """Download (or ensure cached) the faster-whisper model for `size`.

    Reports progress via progress_cb(fraction_completed, bytes_done). Returns the
    local directory containing the model, which can be passed to WhisperModel().
    """
    repo = _MODEL_REPOS.get(size, "small")
    base_url = f"https://huggingface.co/{repo}/resolve/main"
    d = _model_local_dir(size)
    os.makedirs(d, exist_ok=True)

    for name in _MODEL_FILES:
        dest = os.path.join(d, name)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            continue  # already cached
        tmp = dest + ".part"
        _download_file(f"{base_url}/{name}", tmp, progress_cb)
        os.replace(tmp, dest)
    return d


def load_model(size=None):
    # EchoTray always runs on CPU (int8). No GPU/device selection - this keeps
    # it working identically on any machine, including ones with an NVIDIA GPU
    # where CUDA/float16 can cause problems.
    if size is None:
        size = MODEL_SIZE
    device = "cpu"
    compute_type = COMPUTE_TYPE
    if compute_type == "float16":
        print("  Warning: float16 requires a CUDA GPU. EchoTray runs on CPU - using int8.")
        compute_type = "int8"

    print(f"Loading Whisper model ({size}, device=cpu, compute={compute_type})... ", end="", flush=True)
    start = time.monotonic()

    # Load from our local cache if present; otherwise let faster-whisper download.
    local_dir = _model_local_dir(size)
    if os.path.isdir(local_dir) and os.path.isfile(os.path.join(local_dir, "model.bin")):
        model = WhisperModel(local_dir, device=device, compute_type=compute_type)
    else:
        model = WhisperModel(size, device=device, compute_type=compute_type)

    elapsed = time.monotonic() - start
    print(f"done ({elapsed:.1f}s)")
    return model


# ── Audio Recorder ────────────────────────────────────────────────────────────

class AudioRecorder:
    def __init__(self):
        self.chunks = []
        self.stream = None

    def _callback(self, indata, _frames, _time_info, status):
        if status:
            print(f"  Audio warning: {status}")
        self.chunks.append(indata.copy())

    def start(self):
        self.chunks = []
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=self._callback,
            blocksize=1024,
        )
        self.stream.start()

    def stop(self):
        self.stream.stop()
        self.stream.close()
        self.stream = None
        if not self.chunks:
            return np.array([], dtype="float32")
        audio = np.concatenate(self.chunks, axis=0)
        self.chunks.clear()
        return audio.flatten()


# ── Transcription ─────────────────────────────────────────────────────────────

def transcribe_audio(model, audio_data):
    """Transcribe audio with Whisper. Returns the text string (may be empty). Raises on failure."""
    # Serialize transcription: CTranslate2's transcribe() is not thread-safe on
    # a shared model and can deadlock if two calls overlap. The lock ensures only
    # one transcription runs at a time.
    with _TRANSCRIBE_LOCK:
        segments, _info = model.transcribe(
            audio_data,
            language=LANGUAGE,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
    # Always append a trailing space so the user can keep typing the next
    # sentence without pressing the space bar, and consecutive dictations
    # don't run together. (Punctuation is left to Whisper - it occasionally
    # omits a period, but forcing one can produce wrong punctuation.)
    if text:
        text += " "
    return text
