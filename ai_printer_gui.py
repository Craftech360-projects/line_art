"""AI Printer device test client — simple GUI.

A small Tkinter window to test the Cheeko device WebSocket protocol by voice:
  [Start recording]  →  speak  →  [Stop & print]  →  the printed line-art
  bitmap appears in the window.

It does the real device handshake, captures mic audio at 16 kHz, encodes to RAW
Opus frames (PyAV — bare packets, like the firmware), streams listen
start/frames/stop, and renders the returned `line_art` bitmap.

Run with the Python that has sounddevice + av + websockets + pillow (the
miniconda base env here, NOT the env\ venv):

  python ai_printer_gui.py
  python ai_printer_gui.py --url ws://192.168.0.186:8090/ws

Reuses the encoding/handshake helpers from ai_printer_client.py.
"""
import argparse
import asyncio
import base64
import threading
import tkinter as tk
from tkinter import ttk

import numpy as np

from ai_printer_client import pcm_to_opus_frames, SAMPLE_RATE, FRAME_SAMPLES
import json
import websockets
from PIL import Image, ImageTk

try:
    import sounddevice as sd
except ImportError:
    sd = None


class PrinterGUI:
    def __init__(self, root, url):
        self.root = root
        self.url = url
        self._chunks = []
        self._stream = None
        self.recording = False

        root.title("AI Printer — Device Test Client")
        root.configure(bg="#0f1115")
        root.geometry("460x620")

        pad = {"padx": 12, "pady": 6}
        head = tk.Label(root, text="AI Printer — Device Test", fg="#e6e6e6", bg="#0f1115",
                        font=("Segoe UI", 14, "bold"))
        head.pack(**pad)
        self.url_lbl = tk.Label(root, text=url, fg="#8a8f98", bg="#0f1115", font=("Segoe UI", 9))
        self.url_lbl.pack()

        btns = tk.Frame(root, bg="#0f1115")
        btns.pack(pady=10)
        self.rec_btn = tk.Button(btns, text="● Start recording", width=18, height=2,
                                 bg="#2d6cdf", fg="white", relief="flat",
                                 font=("Segoe UI", 10, "bold"), command=self.toggle_record)
        self.rec_btn.grid(row=0, column=0, padx=6)

        self.status = tk.Label(root, text="ready", fg="#81c784", bg="#0f1115", font=("Segoe UI", 10))
        self.status.pack(pady=4)
        self.prog = ttk.Progressbar(root, mode="indeterminate", length=320)

        # Printer canvas.
        self.canvas = tk.Label(root, bg="white", width=384, height=384)
        self.canvas.pack(pady=12)
        self.meta = tk.Label(root, text="", fg="#8a8f98", bg="#0f1115", font=("Segoe UI", 9))
        self.meta.pack()

        self._photo = None  # keep a ref so Tk doesn't GC the image

        if sd is None:
            self.set_status("sounddevice not installed — run under miniconda python", "#ef5350")
            self.rec_btn.config(state="disabled")

    # ---- UI helpers (always call from the main thread via root.after) ----
    def set_status(self, text, color="#cfcfcf"):
        self.root.after(0, lambda: self.status.config(text=text, fg=color))

    def set_meta(self, text):
        self.root.after(0, lambda: self.meta.config(text=text))

    def busy(self, on):
        def _do():
            if on:
                self.prog.pack(pady=4)
                self.prog.start(12)
            else:
                self.prog.stop()
                self.prog.pack_forget()
        self.root.after(0, _do)

    # ---- recording ----
    def toggle_record(self):
        if not self.recording:
            self.start_record()
        else:
            self.stop_record()

    def start_record(self):
        self._chunks = []
        self.recording = True
        self.rec_btn.config(text="■ Stop & print", bg="#c0392b")
        self.set_status("● recording — speak now…", "#ef5350")

        def cb(indata, frames, time_info, status):
            self._chunks.append(indata.copy().reshape(-1))

        self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=cb)
        self._stream.start()

    def stop_record(self):
        self.recording = False
        self.rec_btn.config(text="● Start recording", bg="#2d6cdf", state="disabled")
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        pcm = np.concatenate(self._chunks).astype(np.int16) if self._chunks else np.zeros(0, np.int16)
        if len(pcm) < FRAME_SAMPLES:
            self.set_status("no audio captured", "#ef5350")
            self.rec_btn.config(state="normal")
            return
        self.set_status(f"sending {len(pcm)/SAMPLE_RATE:.1f}s of audio…")
        self.busy(True)
        # Run the network pipeline off the UI thread.
        threading.Thread(target=lambda: asyncio.run(self._pipeline(pcm)), daemon=True).start()

    # ---- websocket pipeline ----
    async def _pipeline(self, pcm):
        try:
            async with websockets.connect(self.url, max_size=None, open_timeout=10) as ws:
                await ws.send(json.dumps({
                    "type": "hello", "version": 1, "features": {"mcp": True}, "transport": "websocket",
                    "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
                }))
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                sid = reply.get("session_id")
                self.set_status(f"handshake OK (session {str(sid)[:8]})")

                frames = pcm_to_opus_frames(pcm)
                await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": "manual"}))
                for f in frames:
                    await ws.send(f)
                await ws.send(json.dumps({"session_id": sid, "type": "listen", "state": "stop"}))

                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=600))
                    t = msg.get("type")
                    if t == "line_art_transcription":
                        self.set_status(f'heard: "{msg["text"]}"')
                    elif t == "line_art_progress":
                        self.set_status(f'[{msg.get("stage")}] {msg.get("message")}')
                    elif t == "line_art_error":
                        self.set_status(f'error: {msg.get("message")}', "#ef5350")
                        break
                    elif t == "line_art":
                        self._show_bitmap(msg["raw_mono"], msg["width"], msg["height"])
                        self.set_status("✓ printed", "#81c784")
                        break
        except Exception as e:
            self.set_status(f"failed: {e}", "#ef5350")
        finally:
            self.busy(False)
            self.root.after(0, lambda: self.rec_btn.config(state="normal"))

    # ---- render the 1-bit bitmap ----
    def _show_bitmap(self, raw_b64, width, height):
        raw = base64.b64decode(raw_b64)
        bpr = width // 8
        img = Image.new("1", (width, height), 1)
        px = img.load()
        for y in range(height):
            for x in range(width):
                bit = (raw[y * bpr + (x >> 3)] >> (7 - (x & 7))) & 1
                px[x, y] = 0 if bit else 1
        photo = ImageTk.PhotoImage(img.convert("L"))

        def _set():
            self._photo = photo
            self.canvas.config(image=photo, width=width, height=height)
            self.meta.config(text=f"{width}×{height} · 1-bit · {len(raw)} bytes printed")
        self.root.after(0, _set)


def main():
    ap = argparse.ArgumentParser(description="AI Printer device test client (GUI)")
    ap.add_argument("--url", default="ws://192.168.0.186:8090/ws")
    args = ap.parse_args()
    root = tk.Tk()
    PrinterGUI(root, args.url)
    root.mainloop()


if __name__ == "__main__":
    main()
