"""End-to-end test: drive the offline pipeline over the WebSocket.

Sends a text prompt, collects progress/result, decodes the 1-bit bitmap and the
PNG preview, and asserts the raw_mono format matches the device contract.
"""
import asyncio
import base64
import json
import sys

import websockets

WS_URL = "ws://localhost:8090/ws"


async def run_text(subject: str, timeout_s: float = 300.0):
    async with websockets.connect(WS_URL, max_size=None, open_timeout=10) as ws:
        await ws.send(json.dumps({"type": "text_input", "text": subject}))
        print(f"-> sent text_input: {subject!r}")
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "progress":
                print(f"   progress[{msg.get('stage')}]: {msg.get('message')}")
            elif t == "error":
                print(f"!! ERROR[{msg.get('stage')}]: {msg.get('message')}")
                return False
            elif t == "result":
                png = base64.b64decode(msg["image"].split(",", 1)[1])
                raw_mono = base64.b64decode(msg["raw_mono"])
                w, h = msg["width"], msg["height"]
                print(f"<- result: {w}x{h}, png={len(png)}B, raw_mono={len(raw_mono)}B")
                print(f"   prompt_used: {msg['prompt_used'][:70]}...")
                # Validate the device contract.
                assert w == 384, f"width {w} != 384"
                assert len(raw_mono) == h * 48, f"raw_mono {len(raw_mono)} != {h}*48"
                assert png[:8] == b"\x89PNG\r\n\x1a\n", "preview is not a PNG"
                # Count black pixels so we know it's a real image, not all-white.
                black = sum(bin(b).count("1") for b in raw_mono)
                total = w * h
                print(f"   black pixels: {black}/{total} ({100*black/total:.1f}%)")
                assert 0 < black < total, "bitmap is blank/all-black (suspicious)"
                # Save artifacts for inspection.
                with open("e2e_preview.png", "wb") as f:
                    f.write(png)
                with open("e2e_raw.bin", "wb") as f:
                    f.write(raw_mono)
                print("   saved e2e_preview.png + e2e_raw.bin")
                print("PASS")
                return True


if __name__ == "__main__":
    subject = sys.argv[1] if len(sys.argv) > 1 else "a cat"
    ok = asyncio.run(run_text(subject))
    sys.exit(0 if ok else 1)
