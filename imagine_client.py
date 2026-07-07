"""Drive line_art's device WS protocol in AI-Imagine mode with a WAV file.
Reuses the repo test client's opus encoding. Saves the returned JPEG."""
import asyncio, base64, json, sys, time

sys.path.insert(0, r"D:\line_art")
from ai_printer_client import load_wav_16k_mono, pcm_to_opus_frames  # noqa: E402
import websockets  # noqa: E402

URL = "ws://127.0.0.1:8090/ws"


async def imagine(wav_path: str, out_jpg: str):
    pcm = load_wav_16k_mono(wav_path)
    frames = pcm_to_opus_frames(pcm)
    print(f"encoded {len(frames)} opus frames from {wav_path}")

    async with websockets.connect(URL, max_size=None, open_timeout=10) as ws:
        await ws.send(json.dumps({
            "type": "hello", "version": 1, "transport": "websocket",
            "feature": "ai_imagine",
            "audio_params": {"format": "opus", "sample_rate": 16000,
                             "channels": 1, "frame_duration": 60},
        }))
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        print("<<< hello reply:", reply)
        sid = reply["session_id"]

        await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": "manual"}))
        for f in frames:
            await ws.send(f)
        await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "stop"}))
        t0 = time.time()
        print("=== listen stop sent, awaiting result...")

        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=180))
            t = msg.get("type")
            if t == "line_art_transcription":
                print(f"<<< transcription ({time.time()-t0:.1f}s): {msg['text']!r}")
            elif t == "line_art_progress":
                print(f"<<< progress[{msg.get('stage')}]: {msg.get('message')}")
            elif t == "line_art_error":
                print(f"!!! error[{msg.get('stage')}]: {msg.get('message')}")
                return 1
            elif t == "image":
                jpeg = base64.b64decode(msg["image"])
                with open(out_jpg, "wb") as fh:
                    fh.write(jpeg)
                print(f"<<< image ({time.time()-t0:.1f}s): {len(jpeg)} bytes, "
                      f"{msg.get('width')}x{msg.get('height')}, caption={msg.get('caption')!r} -> {out_jpg}")
                return 0
            else:
                print("<<< other:", msg)


async def probe_short_utterance():
    """Probe: 2 frames (~120ms) must trip the MIN_UTTERANCE_FRAMES guard, no STT call."""
    async with websockets.connect(URL, max_size=None, open_timeout=10) as ws:
        await ws.send(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
                                  "feature": "ai_imagine"}))
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        sid = reply["session_id"]
        await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "start"}))
        pcm = load_wav_16k_mono(r"D:\line_art\test.wav")
        for f in pcm_to_opus_frames(pcm)[:2]:
            await ws.send(f)
        await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "stop"}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        print("PROBE short-utterance ->", msg)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "imagine"
    if mode == "probe":
        asyncio.run(probe_short_utterance())
    else:
        rc = asyncio.run(imagine(r"D:\line_art\test.wav",
                                 sys.argv[2] if len(sys.argv) > 2 else "imagine_result.jpg"))
        sys.exit(rc or 0)
