"""Piper TTS wrapper (offline). Meta has no TTS API.

The renderer calls Piper directly today; this wrapper gives the voice tutor
the same voice without going through the renderer.
"""
from __future__ import annotations

import io
import subprocess
import sys
import threading
import wave
from pathlib import Path

import numpy as np

VOICE_PATH = Path(__file__).resolve().parent.parent / "renderer" / "voice.onnx"


def available() -> bool:
    return VOICE_PATH.exists()


def speak_to_array(text: str) -> tuple[np.ndarray, int]:
    """Return (int16 samples, sample_rate). Raises FileNotFoundError without the voice model."""
    if not available():
        raise FileNotFoundError(
            f"Piper voice model missing at {VOICE_PATH}. See README.md for the download command."
        )
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".wav") as tf:
        subprocess.run(
            [sys.executable, "-m", "piper", "-m", str(VOICE_PATH), "-f", tf.name, "--length-scale", "1.12"],
            input=text.encode(), capture_output=True, check=True,
        )
        with wave.open(tf.name) as w:
            sr = w.getframerate()
            samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return samples, sr


_voice = None
_voice_lock = threading.Lock()


def speak_wav(text: str) -> bytes:
    """WAV bytes for one line, synthesised in-process (~0.05 s a sentence once the voice is loaded)."""
    global _voice
    from piper import PiperVoice, SynthesisConfig

    with _voice_lock:  # one ONNX session, loaded once, used by one lesson at a time
        if _voice is None:
            if not available():
                raise FileNotFoundError(f"Piper voice model missing at {VOICE_PATH}.")
            _voice = PiperVoice.load(str(VOICE_PATH))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            _voice.synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=1.12))
    return buf.getvalue()


def speak_compressed(text: str) -> tuple[bytes, str]:
    """(audio bytes, MIME type) for one line: MP3 at 48 kbps when ffmpeg is on PATH, else the WAV.

    Measured Nov 2: a 10 s line is 445 KB as WAV (593 KB base64) and 61 KB as MP3; encoding takes
    30-50 ms. MP3 plays everywhere, including iPad Safari. (Opus was smaller but took up to 94 ms.)
    """
    wav = speak_wav(text)
    try:
        mp3 = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-ac", "1", "-f", "mp3", "-b:a", "48k", "pipe:1"],
                             input=wav, capture_output=True, check=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):  # no ffmpeg: the WAV still plays
        return wav, "audio/wav"
    return (mp3, "audio/mpeg") if mp3 else (wav, "audio/wav")
