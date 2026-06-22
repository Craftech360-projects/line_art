"""AI Printer device test client (WebSocket variant).

Faithfully emulates the Cheeko firmware's WebSocket protocol against our local
server, the way the reference MQTT client (client.py) does for the MQTT/UDP
variant — but here over a single WebSocket:

  1. open ws, send the device `hello`, wait for the server `hello` reply
  2. press-and-hold the mic: capture 16 kHz mono PCM, encode to RAW Opus packets
     (60 ms frames, no Ogg — exactly what the device sends), stream as binary
     frames between `listen start` and `listen stop`
  3. receive line_art_transcription / line_art_progress / line_art and save the
     printed 1-bit bitmap as a PNG (and open it)

Opus encoding uses PyAV (libopus) — the same raw-packet format the firmware and
the reference opuslib client use. Mic capture uses sounddevice.

Usage:
  python ai_printer_client.py                 # interactive: Enter to talk, Enter to stop
  python ai_printer_client.py --wav speech.wav  # send a WAV file instead of the mic
  python ai_printer_client.py --url ws://192.168.0.186:8090/ws
"""
import argparse
import asyncio
import base64
import io
import json
import sys
import wave

import numpy as np

try:
    import av
except ImportError:
    sys.exit("PyAV (av) is required: pip install av")
try:
    import websockets
except ImportError:
    sys.exit("websockets is required: pip install websockets")

SAMPLE_RATE = 16000
FRAME_SAMPLES = 960  # 60 ms at 16 kHz, like the device


# --------------------------------------------------------------------------- #
# Opus encoding (PCM16 mono -> list of RAW Opus packets, no Ogg container)
# --------------------------------------------------------------------------- #
def pcm_to_opus_frames(pcm: np.ndarray) -> list[bytes]:
    enc = av.CodecContext.create("libopus", "w")
    enc.sample_rate = SAMPLE_RATE
    enc.format = "s16"
    enc.layout = "mono"
    frames = []
    for i in range(0, len(pcm) - FRAME_SAMPLES, FRAME_SAMPLES):
        chunk = pcm[i:i + FRAME_SAMPLES]
        frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = i
        for pkt in enc.encode(frame):
            frames.append(bytes(pkt))
    for pkt in enc.encode(None):
        frames.append(bytes(pkt))
    return frames


# --------------------------------------------------------------------------- #
# Audio sources
# --------------------------------------------------------------------------- #
def load_wav_16k_mono(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        pcm = np.frombuffer(w.readframes(n), dtype=np.int16)
    if w.getnchannels() == 2:
        pcm = pcm[::2]
    if sr != SAMPLE_RATE:  # naive resample, fine for a test
        idx = (np.arange(int(len(pcm) * SAMPLE_RATE / sr)) * sr / SAMPLE_RATE).astype(int)
        pcm = pcm[idx[idx < len(pcm)]]
    return pcm.astype(np.int16)


def record_until_enter() -> np.ndarray:
    """Capture mic audio at 16 kHz mono until the user presses Enter."""
    try:
        import sounddevice as sd
    except ImportError:
        sys.exit("sounddevice is required for mic capture: pip install sounddevice")

    import threading
    chunks, stop = [], threading.Event()

    def cb(indata, frames, time_info, status):
        chunks.append(indata.copy().reshape(-1))

    print(">>> Recording — speak now, then press Enter to stop…")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=cb):
        input()
        stop.set()
    if not chunks:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(chunks).astype(np.int16)


# --------------------------------------------------------------------------- #
# Bitmap output
# --------------------------------------------------------------------------- #
def save_bitmap(raw_mono_b64: str, width: int, height: int, path: str = "printed.png"):
    raw = base64.b64decode(raw_mono_b64)
    bytes_per_row = width // 8
    try:
        from PIL import Image
        img = Image.new("1", (width, height), 1)  # 1 = white
        px = img.load()
        for y in range(height):
            for x in range(width):
                byte = raw[y * bytes_per_row + (x >> 3)]
                bit = (byte >> (7 - (x & 7))) & 1  # MSB-first, 1=black
                px[x, y] = 0 if bit else 1
        img.save(path)
        print(f"<<< saved printed bitmap -> {path}  ({width}x{height})")
        return path
    except ImportError:
        with open("printed.raw", "wb") as f:
            f.write(raw)
        print("<<< saved printed.raw (Pillow not installed for PNG)")
        return "printed.raw"


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
async def run(url: str, wav: str | None, out: str):
    async with websockets.connect(url, max_size=None, open_timeout=10) as ws:
        # 1. Handshake — send the exact device hello.
        hello = {
            "type": "hello", "version": 1, "features": {"mcp": True}, "transport": "websocket",
            "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
        }
        await ws.send(json.dumps(hello))
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if reply.get("type") != "hello" or reply.get("transport") != "websocket":
            sys.exit(f"bad hello reply: {reply}")
        sid = reply.get("session_id")
        print(f"=== handshake OK — session_id={sid}")

        # 2. Get audio (file or mic) and encode to raw Opus frames.
        pcm = load_wav_16k_mono(wav) if wav else record_until_enter()
        if len(pcm) < FRAME_SAMPLES:
            sys.exit("no/too little audio captured")
        frames = pcm_to_opus_frames(pcm)
        print(f"=== encoded {len(frames)} raw Opus frames ({len(pcm)/SAMPLE_RATE:.1f}s)")

        # 3. listen start -> stream frames -> listen stop.
        await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": "manual"}))
        for f in frames:
            await ws.send(f)
        await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "stop"}))
        print("=== sent listen stop — awaiting line_art (cold ComfyUI may take minutes)…")

        # 4. Receive results.
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=600))
            t = msg.get("type")
            if t == "line_art_transcription":
                print(f"<<< transcription: {msg['text']!r}")
            elif t == "line_art_progress":
                print(f"<<< progress[{msg.get('stage')}]: {msg.get('message')}")
            elif t == "line_art_error":
                print(f"!!! error[{msg.get('stage')}]: {msg.get('message')}")
                return
            elif t == "line_art":
                save_bitmap(msg["raw_mono"], msg["width"], msg["height"], out)
                print("=== DONE")
                return


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AI Printer device test client (WebSocket)")
    ap.add_argument("--url", default="ws://192.168.0.186:8090/ws")
    ap.add_argument("--wav", default=None, help="send a WAV file instead of mic capture")
    ap.add_argument("--out", default="printed.png", help="where to save the printed bitmap")
    args = ap.parse_args()
    try:
        asyncio.run(run(args.url, args.wav, args.out))
    except KeyboardInterrupt:
        print("\ninterrupted")
