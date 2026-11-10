import os
import subprocess
import tempfile
import threading
import time

import imageio_ffmpeg
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("GNANI_API_KEY")
BASE = "https://api.vachana.ai"
HEADERS = {"X-API-Key-ID": API_KEY}

# Gnani may allow only 1 active request at a time, so we send one call at a time.
_lock = threading.Lock()


def _post(path, **kwargs):
    r = None
    for attempt in range(4):
        with _lock:
            r = requests.post(BASE + path, headers=HEADERS, timeout=120, **kwargs)
        if r.status_code != 429:
            return r
        time.sleep(2 * (attempt + 1))  # rate limited: wait, then retry
    return r


def to_wav(raw: bytes) -> bytes:
    """Convert any browser recording (webm/mp4/ogg) to 16 kHz mono WAV."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory() as folder:
        src = os.path.join(folder, "input_audio")
        dst = os.path.join(folder, "output.wav")
        with open(src, "wb") as f:
            f.write(raw)
        result = subprocess.run(
            [ffmpeg, "-y", "-i", src, "-t", "28", "-ar", "16000",
             "-ac", "1", "-sample_fmt", "s16", dst],
            capture_output=True,
        )
        if result.returncode != 0 or not os.path.exists(dst):
            raise RuntimeError("Could not read the recorded audio")
        with open(dst, "rb") as f:
            return f.read()


def stt(wav: bytes) -> str:
    r = _post(
        "/stt/v3",
        files={"audio_file": ("audio.wav", wav, "audio/wav")},
        data={"model": "prisma-v3.0", "language_code": "hi-IN,en-IN", "format": "transcribe"},
    )
    if r.status_code != 200:
        raise RuntimeError(f"Speech-to-text failed ({r.status_code}): {r.text[:200]}")
    return r.json().get("transcript", "").strip()


def make_embedding(wav: bytes) -> dict:
    r = _post(
        "/api/v1/tts/voice-clone/embeddings",
        files={"audio_file": ("voice.wav", wav, "audio/wav")},
    )
    if r.status_code != 200:
        raise RuntimeError(f"Voice cloning failed ({r.status_code}): {r.text[:200]}")
    return r.json()["data"]["voice_clone_embedding"]


def speak(text: str, embedding: dict) -> bytes:
    payload = {
        "text": text,
        "model": "vachana-vc-v1",
        "speaker_embedding": embedding,
        "audio_config": {
            "sample_rate": 44100,
            "num_channels": 1,
            "sample_width": 2,
            "encoding": "linear_pcm",
            "container": "wav",
        },
    }
    r = _post("/api/v1/tts/inference", json=payload)
    if r.status_code != 200:
        raise RuntimeError(f"Text-to-speech failed ({r.status_code}): {r.text[:200]}")
    return r.content