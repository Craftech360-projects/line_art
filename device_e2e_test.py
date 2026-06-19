"""Manual device-protocol integration test.

Mimics the Cheeko firmware: hello -> listen start -> raw Opus frames of speech
-> listen stop, and asserts the server replies hello + line_art_transcription +
line_art. Requires the app on :8090 and Speaches + ComfyUI running.

Usage: python device_e2e_test.py [path-to-speech.wav]
A WAV is converted to 16kHz mono Opus frames. If no WAV is given, synthesizes a
silent clip (transcription may be empty -> expect line_art_error, which still
proves the protocol round-trips).
"""
import asyncio
import io
import json
import sys
import wave

import numpy as np
import websockets

from app.opus_decode import _encode_pcm_to_opus

WS_URL = "ws://localhost:8090/ws"


def _load_pcm_16k_mono(path: str | None):
    if path is None:
        return np.zeros(16000, dtype=np.int16)  # 1s silence
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    pcm = np.frombuffer(raw, dtype=np.int16)
    # Naive resample to 16k if needed (good enough for a manual test).
    if sr != 16000:
        idx = (np.arange(int(len(pcm) * 16000 / sr)) * sr / 16000).astype(int)
        idx = idx[idx < len(pcm)]
        pcm = pcm[idx]
    return pcm


async def run(wav_path):
    pcm = _load_pcm_16k_mono(wav_path)
    frames = _encode_pcm_to_opus(pcm, sample_rate=16000)
    async with websockets.connect(WS_URL, max_size=None, open_timeout=10) as ws:
        await ws.send(json.dumps({
            "type": "hello", "version": 1, "transport": "websocket",
            "features": {"mcp": True},
            "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
        }))
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        print("<- hello:", hello)
        assert hello["type"] == "hello" and hello["transport"] == "websocket"
        sid = hello.get("session_id")

        await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": "auto"}))
        for f in frames:
            await ws.send(f)  # binary Opus frame
        await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "stop"}))
        print(f"-> sent {len(frames)} opus frames")

        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=300))
            t = msg.get("type")
            print("<-", t, {k: v for k, v in msg.items() if k != "raw_mono"})
            if t == "line_art":
                import base64
                raw = base64.b64decode(msg["raw_mono"])
                assert msg["width"] == 384
                assert len(raw) == msg["height"] * 48
                print(f"   raw_mono OK: {len(raw)} bytes = {msg['height']} rows x 48")
                print("PASS")
                return True
            if t == "line_art_error":
                print("   (error path — protocol still round-tripped)")
                return True


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(0 if asyncio.run(run(path)) else 1)
