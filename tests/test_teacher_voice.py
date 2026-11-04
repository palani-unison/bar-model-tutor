"""The optional cloud teacher voice (TEACHER_VOICE=resemble): off by default, falls back to Piper on any failure."""
import base64
import io
import json

from voice import tts


def test_off_by_default_never_calls_out(monkeypatch):
    monkeypatch.delenv("TEACHER_VOICE", raising=False)
    monkeypatch.setenv("RESEMBLE_API_KEY", "k")
    monkeypatch.setenv("RESEMBLE_TEACHER_VOICE", "v")
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert tts._resemble_wav("hello") is None


def test_on_uses_resemble_and_falls_back(monkeypatch):
    monkeypatch.setenv("TEACHER_VOICE", "resemble")
    monkeypatch.setenv("RESEMBLE_API_KEY", "k")
    monkeypatch.setenv("RESEMBLE_TEACHER_VOICE", "v")
    import urllib.request
    seen = {}

    class Res(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout):
        seen["body"] = json.loads(req.data)
        return Res(json.dumps({"audio_content": base64.b64encode(b"RIFFwav").decode()}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert tts._resemble_wav("Good try!") == b"RIFFwav" and seen["body"]["voice_uuid"] == "v"
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert tts._resemble_wav("Good try!") is None  # Piper takes over
