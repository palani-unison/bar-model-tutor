"""Muse Voice Transcribe realtime WebSocket client.

Contract: 24 kHz mono 16-bit little-endian PCM in (binary frames, paced at about
real time), transcript events out. One stream per lesson in ENDPOINTING mode:
the model finds where each of the child's turns ends, so they just talk.
Measured Nov 2: ~1.6 s to open, final text ~0.4 s after speech ends.
Raw audio is never stored — frames pass straight through (child safety / PDPA).
Docs: https://dev.meta.ai/docs/speech-to-text.md
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from agent import client as _client  # noqa: F401 — loads .env

logger = logging.getLogger("bar_model_tutor.asr")

WS_URL = "wss://api.meta.ai/v1/asr/realtime"
SAMPLE_RATE = 24000
MODEL = "muse-voice-transcribe-1.0"


class TranscribeStream:
    """One realtime transcription session. Feed PCM with send(); read events from events()."""

    def __init__(self, keywords: list[str] | None = None, mode: str = "ENDPOINTING"):
        self.keywords = [k for k in (keywords or []) if k][:50]
        self.mode = mode
        self._ws = None

    async def open(self) -> None:
        import websockets

        key = os.environ.get("META_API_KEY", "").strip()
        if not key:
            raise _client.MissingKeyError("META_API_KEY is not set, so voice input is off. Type your answer instead.")
        self._ws = await websockets.connect(WS_URL, open_timeout=10, max_size=2**20)
        handshake = {
            "authorization": {"accessToken": f"Bearer {key}"},
            "audioEncoding": "PCM_24KHZ",
            "model": MODEL,
            "mode": self.mode,
            "partialMode": "CUMULATIVE",
            "emitAudioProgress": False,
            "languageBias": ["English"],
        }
        if self.keywords:
            handshake["keywords"] = self.keywords
        await self._ws.send(json.dumps(handshake))
        ack = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=10))
        if "sessionId" not in ack:
            raise _client.ModelUnavailableError(f"voice service refused the session: {ack.get('message', ack)}")
        logger.info("asr session %s open", ack["sessionId"])

    async def send(self, pcm: bytes) -> None:
        if self._ws is not None:
            await self._ws.send(pcm)

    async def events(self):
        """Yield {"type": "partial"|"final", "text": str}. Ends when the server closes the stream."""
        async for message in self._ws:
            if not isinstance(message, str):
                continue
            event = json.loads(message)
            kind = event.get("type")
            if kind == "transcript" and event.get("transcript"):
                yield {"type": "final" if event.get("final") and self.mode == "PUSH_TO_TALK" else "partial",
                       "text": event["transcript"]}
            elif kind == "speechComplete":
                yield {"type": "final", "text": event.get("transcript", "")}
            elif kind == "error":
                logger.warning("asr error: %s", event.get("message"))
                yield {"type": "error", "text": event.get("message", "voice error")}

    async def end(self) -> None:
        """No more audio. The server sends any last transcript, then closes; keep reading events()."""
        if self._ws is not None:
            await self._ws.send(json.dumps({"type": "endStream"}))

    async def close(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({"type": "endStream"}))
        except Exception:  # noqa: BLE001 — already closed
            pass
        await self._ws.close()
        self._ws = None
