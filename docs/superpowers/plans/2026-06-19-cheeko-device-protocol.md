# Cheeko Device Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/ws` speak the AI Printer Cheeko firmware protocol (hello handshake, raw Opus audio, `line_art_*` print messages) so the device connects, sends voice, and prints the FLUX bitmap — while the existing browser protocol keeps working.

**Architecture:** `/ws` peeks the first message: a `hello` routes to a new device-session handler; anything else falls through to the existing browser handlers untouched. The device handler decodes raw Opus frames → WAV (PyAV) → existing `stt.transcribe`, then runs the existing `generate_line_art` and streams the device's `line_art_*` messages. No new external dependency (PyAV `av` already installed).

**Tech Stack:** Python 3.11, FastAPI WebSocket (low-level `ws.receive()`), PyAV (`av`) for Opus decode, existing Speaches STT + ComfyUI image pipeline, pytest (+ pytest-asyncio).

## Global Constraints

- Source of truth for the wire protocol: `aiprinter-server-contract.md` (derived from firmware). Use its exact message `type` strings and field names.
- Server **must** reply to the device `hello` with `{"type":"hello","transport":"websocket",...}` (transport value MUST be the string `websocket`) immediately (well within the device's 10 s timeout).
- Device audio is **raw Opus** binary frames (protocol v1, no Ogg, no header), 16 kHz mono, 60 ms. Decode with `av.CodecContext.create("libopus","r")` fed bare `av.packet.Packet(bytes)` — NO Ogg demuxer.
- Outgoing device messages MUST include `session_id` when the server assigned one in the hello.
- Cheeko print message types (exact): `line_art_transcription` (`text`), `line_art_progress` (`message` required, `stage` optional), `line_art_error` (`message` required, `stage` optional), `line_art` (`raw_mono` required, `width`, `height`). Send progress first, then exactly one `line_art` OR `line_art_error`.
- End-of-utterance is the device's `{"type":"listen","state":"stop"}`. A channel close while audio is buffered triggers a best-effort flush.
- NO TTS audio, NO MCP, NO RFID card handling, NO binary protocol v2/v3, NO VAD (out of scope).
- The existing browser protocol (`text_input` JSON, raw WAV bytes) and its tests MUST remain green. Do not modify `app/stt.py`, `app/image_gen.py`, or `app/models.py` behavior.
- No new pip dependency: use `av`, `wave`, `numpy` (all already installed). Do not add anything to `requirements.txt` except the runtime deps the app already needs (PyAV — see Task 5 note).
- Tests must not hit real Speaches/ComfyUI; mock `stt.transcribe` and `image_gen.generate_line_art`.

---

### Task 1: Opus → WAV decode module

**Files:**
- Create: `app/opus_decode.py`
- Create: `tests/test_opus_decode.py`

**Interfaces:**
- Consumes: nothing (uses `av`, `wave`, `numpy`).
- Produces: `decode_opus_to_wav(frames: list[bytes], sample_rate: int = 16000) -> bytes` — decodes a list of raw Opus packets to a PCM16 mono WAV (bytes, RIFF). Raises `ValueError("no audio decoded from opus frames")` if the frames yield zero PCM samples. Also exposes `_encode_pcm_to_opus(pcm: "np.ndarray", sample_rate: int = 16000, frame_samples: int = 960) -> list[bytes]` as a TEST HELPER used to synthesize input (kept in the module so tests don't duplicate encoder setup).

- [ ] **Step 1: Write the failing test `tests/test_opus_decode.py`**

```python
import io
import wave

import numpy as np

from app.opus_decode import decode_opus_to_wav, _encode_pcm_to_opus


def _make_tone(sr=16000, seconds=1.0, hz=440.0):
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    return (np.sin(2 * np.pi * hz * t) * 12000).astype(np.int16)


def test_round_trip_opus_to_wav():
    sr = 16000
    pcm = _make_tone(sr)
    frames = _encode_pcm_to_opus(pcm, sample_rate=sr)
    assert len(frames) > 10  # ~49 frames for 1s at 60ms
    assert all(isinstance(f, bytes) and len(f) > 0 for f in frames)

    wav = decode_opus_to_wav(frames, sample_rate=sr)
    assert wav[:4] == b"RIFF"

    # WAV is parseable, mono, 16-bit, right rate, and roughly 1s of audio.
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == sr
        assert w.getnframes() > sr // 2  # at least half a second decoded


def test_empty_frames_raises():
    import pytest
    with pytest.raises(ValueError, match="no audio decoded"):
        decode_opus_to_wav([], sample_rate=16000)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_opus_decode.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.opus_decode'`

- [ ] **Step 3: Create `app/opus_decode.py`**

```python
"""Decode raw Opus packets (as sent by the Cheeko device) to PCM16 WAV bytes.

The device sends BARE Opus packets over binary WebSocket frames — no Ogg
container, no header. PyAV's libopus decoder consumes these packets directly.
"""
import io
import logging
import wave

import av
import numpy as np

logger = logging.getLogger(__name__)


def decode_opus_to_wav(frames: list[bytes], sample_rate: int = 16000) -> bytes:
    """Decode raw Opus packets to a mono PCM16 WAV (bytes)."""
    decoder = av.CodecContext.create("libopus", "r")
    decoder.sample_rate = sample_rate
    decoder.format = "s16"
    decoder.layout = "mono"

    chunks = []
    for raw in frames:
        packet = av.packet.Packet(raw)
        for frame in decoder.decode(packet):
            chunks.append(frame.to_ndarray().reshape(-1))
    for frame in decoder.decode(None):  # flush decoder
        chunks.append(frame.to_ndarray().reshape(-1))

    if not chunks:
        raise ValueError("no audio decoded from opus frames")

    pcm = np.concatenate(chunks).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _encode_pcm_to_opus(pcm, sample_rate: int = 16000, frame_samples: int = 960) -> list[bytes]:
    """Test helper: encode PCM16 mono ndarray to a list of raw Opus packets."""
    encoder = av.CodecContext.create("libopus", "w")
    encoder.sample_rate = sample_rate
    encoder.format = "s16"
    encoder.layout = "mono"

    packets = []
    for i in range(0, len(pcm) - frame_samples, frame_samples):
        chunk = pcm[i:i + frame_samples]
        frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = sample_rate
        frame.pts = i
        for pkt in encoder.encode(frame):
            packets.append(bytes(pkt))
    for pkt in encoder.encode(None):  # flush
        packets.append(bytes(pkt))
    return packets
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_opus_decode.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/opus_decode.py tests/test_opus_decode.py
git commit -m "feat: decode raw Opus packets to WAV via PyAV"
```

---

### Task 2: Device outgoing-message builders

**Files:**
- Create: `app/device_messages.py`
- Create: `tests/test_device_messages.py`

**Interfaces:**
- Consumes: nothing.
- Produces (all return a `dict` ready to JSON-serialize; each includes `session_id` only when given and non-None):
  - `hello_reply(session_id: str, sample_rate: int = 16000, frame_duration: int = 60) -> dict`
  - `line_art_transcription(text: str, session_id: str | None = None) -> dict`
  - `line_art_progress(message: str, stage: str | None = None, session_id: str | None = None) -> dict`
  - `line_art_error(message: str, stage: str | None = None, session_id: str | None = None) -> dict`
  - `line_art(raw_mono: str, width: int, height: int, session_id: str | None = None) -> dict`

- [ ] **Step 1: Write the failing test `tests/test_device_messages.py`**

```python
from app import device_messages as dm


def test_hello_reply_shape():
    msg = dm.hello_reply("sess-1", sample_rate=16000, frame_duration=60)
    assert msg["type"] == "hello"
    assert msg["transport"] == "websocket"  # firmware REQUIRES this exact value
    assert msg["session_id"] == "sess-1"
    assert msg["audio_params"] == {"sample_rate": 16000, "frame_duration": 60}


def test_transcription_includes_session_id():
    msg = dm.line_art_transcription("a cat", session_id="s2")
    assert msg == {"type": "line_art_transcription", "session_id": "s2", "text": "a cat"}


def test_progress_optional_stage_and_session():
    # No stage, no session -> neither key present.
    assert dm.line_art_progress("working") == {"type": "line_art_progress", "message": "working"}
    # With stage + session.
    msg = dm.line_art_progress("gen", stage="image_gen", session_id="s3")
    assert msg == {
        "type": "line_art_progress", "session_id": "s3",
        "message": "gen", "stage": "image_gen",
    }


def test_error_shape():
    msg = dm.line_art_error("boom", stage="stt", session_id="s4")
    assert msg == {
        "type": "line_art_error", "session_id": "s4", "message": "boom", "stage": "stt",
    }


def test_line_art_shape():
    msg = dm.line_art("AAAA", 384, 240, session_id="s5")
    assert msg == {
        "type": "line_art", "session_id": "s5",
        "raw_mono": "AAAA", "width": 384, "height": 240,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_device_messages.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.device_messages'`

- [ ] **Step 3: Create `app/device_messages.py`**

```python
"""Builders for server->device JSON messages (Cheeko firmware protocol).

Each returns a plain dict. `session_id` is included only when provided so the
device echoes it back. Optional fields are omitted when None.
"""


def _with_session(msg: dict, session_id: str | None) -> dict:
    if session_id is not None:
        # Place session_id right after type for readability.
        return {"type": msg["type"], "session_id": session_id,
                **{k: v for k, v in msg.items() if k != "type"}}
    return msg


def hello_reply(session_id: str, sample_rate: int = 16000, frame_duration: int = 60) -> dict:
    return {
        "type": "hello",
        "transport": "websocket",
        "session_id": session_id,
        "audio_params": {"sample_rate": sample_rate, "frame_duration": frame_duration},
    }


def line_art_transcription(text: str, session_id: str | None = None) -> dict:
    return _with_session({"type": "line_art_transcription", "text": text}, session_id)


def line_art_progress(message: str, stage: str | None = None, session_id: str | None = None) -> dict:
    msg = {"type": "line_art_progress", "message": message}
    if stage is not None:
        msg["stage"] = stage
    return _with_session(msg, session_id)


def line_art_error(message: str, stage: str | None = None, session_id: str | None = None) -> dict:
    msg = {"type": "line_art_error", "message": message}
    if stage is not None:
        msg["stage"] = stage
    return _with_session(msg, session_id)


def line_art(raw_mono: str, width: int, height: int, session_id: str | None = None) -> dict:
    return _with_session(
        {"type": "line_art", "raw_mono": raw_mono, "width": width, "height": height},
        session_id,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_device_messages.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add app/device_messages.py tests/test_device_messages.py
git commit -m "feat: add Cheeko device outgoing-message builders"
```

---

### Task 3: Device session handler

**Files:**
- Create: `app/device_protocol.py`
- Create: `tests/test_device_protocol.py`

**Interfaces:**
- Consumes: `app.opus_decode.decode_opus_to_wav`, `app.device_messages` builders, `app.stt.transcribe`, `app.image_gen.generate_line_art`.
- Produces: `async def handle_device_session(ws, first_message: dict, *, transcribe=stt.transcribe, generate_line_art=image_gen.generate_line_art, decode=opus_decode.decode_opus_to_wav) -> None`.
  - `ws` is a Starlette/FastAPI WebSocket already `accept()`-ed. The handler uses `ws.send_json(dict)` to send and `ws.receive()` to read the low-level `{"type","text"/"bytes"}` envelope.
  - `first_message` is the already-parsed device `hello` dict (the caller peeked it).
  - The mockable `transcribe`/`generate_line_art`/`decode` keyword args exist for tests; production callers use the defaults.
  - `generate_line_art(subject)` returns `(data_uri, prompt_used, raw_mono_b64, height)` (existing signature). The handler sends `line_art` with that `raw_mono_b64`, `width=384`, `height`.

- [ ] **Step 1: Write the failing test `tests/test_device_protocol.py`**

```python
import pytest

from app import device_protocol


class FakeWS:
    """Minimal WebSocket double: scripted receive(), captured send_json()."""

    def __init__(self, events):
        # events: list of dicts shaped like Starlette's ws.receive() output,
        # e.g. {"type":"websocket.receive","text":"..."} or {"...":"bytes":b"..."}
        # or {"type":"websocket.disconnect"} to end the loop.
        self._events = list(events)
        self.sent = []

    async def receive(self):
        if not self._events:
            return {"type": "websocket.disconnect"}
        return self._events.pop(0)

    async def send_json(self, data):
        self.sent.append(data)


def _text(d):
    import json
    return {"type": "websocket.receive", "text": json.dumps(d)}


def _bytes(b):
    return {"type": "websocket.receive", "bytes": b}


@pytest.mark.asyncio
async def test_hello_reply_sent_first():
    ws = FakeWS([])  # no further events; disconnect immediately after hello
    hello = {"type": "hello", "version": 1, "transport": "websocket",
             "audio_params": {"format": "opus", "sample_rate": 16000}}
    await device_protocol.handle_device_session(ws, hello)
    assert ws.sent[0]["type"] == "hello"
    assert ws.sent[0]["transport"] == "websocket"
    assert "session_id" in ws.sent[0]


@pytest.mark.asyncio
async def test_full_listen_cycle_emits_line_art_sequence():
    captured = {}

    async def fake_transcribe(wav_bytes):
        captured["wav"] = wav_bytes
        return "a cat"

    async def fake_generate(subject):
        captured["subject"] = subject
        return ("data:image/png;base64,AAA", f"prompt {subject}", "cmF3bW9ubw==", 240)

    def fake_decode(frames, sample_rate=16000):
        captured["frames"] = list(frames)
        return b"RIFFfakewav"

    events = [
        _text({"type": "listen", "state": "start", "mode": "auto"}),
        _bytes(b"opus1"),
        _bytes(b"opus2"),
        _text({"type": "listen", "state": "stop"}),
    ]
    ws = FakeWS(events)
    hello = {"type": "hello", "transport": "websocket"}
    await device_protocol.handle_device_session(
        ws, hello, transcribe=fake_transcribe, generate_line_art=fake_generate, decode=fake_decode,
    )

    types = [m["type"] for m in ws.sent]
    assert types[0] == "hello"
    assert "line_art_transcription" in types
    assert "line_art_progress" in types
    assert "line_art" in types
    # Order: transcription before progress before final line_art.
    assert types.index("line_art_transcription") < types.index("line_art_progress") < types.index("line_art")

    # Opus frames were collected and decoded; transcript drove generation.
    assert captured["frames"] == [b"opus1", b"opus2"]
    assert captured["wav"] == b"RIFFfakewav"
    assert captured["subject"] == "a cat"

    final = next(m for m in ws.sent if m["type"] == "line_art")
    assert final["raw_mono"] == "cmF3bW9ubw=="
    assert final["width"] == 384
    assert final["height"] == 240
    # session_id echoed on every non-hello message.
    sid = ws.sent[0]["session_id"]
    assert all(m.get("session_id") == sid for m in ws.sent[1:])


@pytest.mark.asyncio
async def test_empty_transcript_emits_error_not_line_art():
    async def empty_transcribe(wav):
        return "   "

    async def fake_generate(subject):  # should not be called
        raise AssertionError("generate must not run on empty transcript")

    events = [
        _text({"type": "listen", "state": "start"}),
        _bytes(b"x"),
        _text({"type": "listen", "state": "stop"}),
    ]
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"}, transcribe=empty_transcribe,
        generate_line_art=fake_generate, decode=lambda f, sample_rate=16000: b"RIFF",
    )
    types = [m["type"] for m in ws.sent]
    assert "line_art_error" in types
    assert "line_art" not in types


@pytest.mark.asyncio
async def test_generate_failure_emits_error():
    async def ok_transcribe(wav):
        return "a dog"

    async def boom_generate(subject):
        raise RuntimeError("ComfyUI unavailable")

    events = [
        _text({"type": "listen", "state": "start"}),
        _bytes(b"x"),
        _text({"type": "listen", "state": "stop"}),
    ]
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"}, transcribe=ok_transcribe,
        generate_line_art=boom_generate, decode=lambda f, sample_rate=16000: b"RIFF",
    )
    err = next(m for m in ws.sent if m["type"] == "line_art_error")
    assert "ComfyUI unavailable" in err["message"]
    assert err["stage"] == "image_gen"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_device_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.device_protocol'`

- [ ] **Step 3: Create `app/device_protocol.py`**

```python
"""Cheeko device WebSocket session: hello handshake, Opus audio buffering,
and the line_art_* print message flow. See aiprinter-server-contract.md.
"""
import json
import logging
import uuid

from starlette.websockets import WebSocketDisconnect

from app import device_messages as dm
from app import opus_decode
from app import stt
from app import image_gen

logger = logging.getLogger(__name__)


async def handle_device_session(
    ws,
    first_message: dict,
    *,
    transcribe=stt.transcribe,
    generate_line_art=image_gen.generate_line_art,
    decode=opus_decode.decode_opus_to_wav,
) -> None:
    """Drive one device session. `first_message` is the parsed device hello."""
    session_id = uuid.uuid4().hex
    await ws.send_json(dm.hello_reply(session_id))
    logger.info("Device session %s started", session_id)

    listening = False
    opus_frames: list[bytes] = []

    try:
        while True:
            message = await ws.receive()
            mtype = message.get("type")
            if mtype == "websocket.disconnect":
                break
            if mtype != "websocket.receive":
                continue

            if "text" in message and message["text"] is not None:
                try:
                    data = json.loads(message["text"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if data.get("type") == "listen":
                    state = data.get("state")
                    if state == "start":
                        listening = True
                        opus_frames = []
                    elif state == "stop":
                        listening = False
                        await _run_line_art(
                            ws, session_id, opus_frames, transcribe, generate_line_art, decode,
                        )
                        opus_frames = []
                # other text types (mcp, hello repeats, etc.) are ignored
            elif "bytes" in message and message["bytes"] is not None:
                if listening:
                    opus_frames.append(message["bytes"])
    except WebSocketDisconnect:
        pass
    finally:
        # Best-effort flush: audio buffered but never stopped.
        if opus_frames and listening:
            try:
                await _run_line_art(
                    ws, session_id, opus_frames, transcribe, generate_line_art, decode,
                )
            except Exception:
                logger.exception("flush failed for session %s", session_id)
    logger.info("Device session %s ended", session_id)


async def _run_line_art(ws, session_id, opus_frames, transcribe, generate_line_art, decode):
    """Decode -> transcribe -> generate -> emit the line_art_* sequence."""
    # 1. Decode + transcribe.
    try:
        wav = decode(opus_frames)
        text = (await transcribe(wav)).strip()
    except Exception as e:
        logger.exception("STT failed")
        await ws.send_json(dm.line_art_error(f"Transcription failed: {e}", stage="stt", session_id=session_id))
        return

    if not text:
        await ws.send_json(dm.line_art_error(
            "Could not transcribe any speech from audio.", stage="stt", session_id=session_id))
        return

    await ws.send_json(dm.line_art_transcription(text, session_id=session_id))
    await ws.send_json(dm.line_art_progress(
        f"Generating line art for '{text}'...", stage="image_gen", session_id=session_id))

    # 2. Generate.
    try:
        _data_uri, _prompt, raw_mono, height = await generate_line_art(text)
    except Exception as e:
        logger.exception("Image generation failed")
        await ws.send_json(dm.line_art_error(str(e), stage="image_gen", session_id=session_id))
        return

    await ws.send_json(dm.line_art(raw_mono, 384, height, session_id=session_id))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_device_protocol.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `python -m pytest -q`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add app/device_protocol.py tests/test_device_protocol.py
git commit -m "feat: add Cheeko device session handler (hello + opus + line_art)"
```

---

### Task 4: Route `/ws` to the device handler on `hello`

**Files:**
- Modify: `app/main.py`
- Create: `tests/test_ws_dispatch.py`

**Interfaces:**
- Consumes: `app.device_protocol.handle_device_session`.
- Produces: updated `/ws` that, on the FIRST message, peeks for a JSON `hello`. If `type == "hello"`, delegate the whole connection to `handle_device_session(ws, parsed)` and return. Otherwise, process that first message AND subsequent ones with the existing browser logic (text_input / binary WAV). Existing browser behavior is preserved.

- [ ] **Step 1: Write the failing test `tests/test_ws_dispatch.py`**

This test exercises the dispatch decision by calling the endpoint function with a FakeWS, monkeypatching the device handler so we don't run the full pipeline.

```python
import json

import pytest

from app import main


class FakeWS:
    def __init__(self, events):
        self._events = list(events)
        self.sent = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive(self):
        if not self._events:
            return {"type": "websocket.disconnect"}
        return self._events.pop(0)

    async def send_text(self, text):
        self.sent.append(text)

    async def send_json(self, data):
        self.sent.append(data)


def _text(d):
    return {"type": "websocket.receive", "text": json.dumps(d)}


@pytest.mark.asyncio
async def test_hello_routes_to_device_handler(monkeypatch):
    called = {}

    async def fake_device(ws, first_message, **kw):
        called["first"] = first_message

    monkeypatch.setattr(main, "handle_device_session", fake_device)

    ws = FakeWS([_text({"type": "hello", "transport": "websocket"})])
    await main.websocket_endpoint(ws)
    assert called["first"]["type"] == "hello"


@pytest.mark.asyncio
async def test_text_input_still_uses_browser_handler(monkeypatch):
    seen = {}

    async def fake_text(ws, subject):
        seen["subject"] = subject

    # If the device handler were wrongly called, fail loudly.
    async def boom_device(ws, first_message, **kw):
        raise AssertionError("device handler should not run for text_input")

    monkeypatch.setattr(main, "handle_text_input", fake_text)
    monkeypatch.setattr(main, "handle_device_session", boom_device)

    ws = FakeWS([_text({"type": "text_input", "text": "a cat"})])
    await main.websocket_endpoint(ws)
    assert seen["subject"] == "a cat"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ws_dispatch.py -v`
Expected: FAIL (`main` has no `handle_device_session`; `/ws` doesn't peek/branch)

- [ ] **Step 3: Edit `app/main.py`**

Add the import near the other app imports (after line 13, `from app.stt import transcribe`):

```python
from app.device_protocol import handle_device_session
```

Replace the entire `websocket_endpoint` function (currently lines ~86-108) with:

```python
async def _process_browser_message(ws: WebSocket, message: dict):
    """Handle one message in the existing browser protocol."""
    if "text" in message and message["text"] is not None:
        try:
            data = json.loads(message["text"])
            parsed = TextInput(**data)
            await handle_text_input(ws, parsed.text)
        except (json.JSONDecodeError, ValueError) as e:
            await send_json(ws, ErrorMessage(stage="input", message=f"Invalid message: {e}"))
    elif "bytes" in message and message["bytes"] is not None:
        await handle_audio_input(ws, message["bytes"])


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    logger.info("WebSocket connected")

    try:
        # Peek the first message to choose protocol: a `hello` => device.
        first = await ws.receive()
        if first.get("type") == "websocket.disconnect":
            return
        if first.get("type") == "websocket.receive" and first.get("text"):
            try:
                parsed = json.loads(first["text"])
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict) and parsed.get("type") == "hello":
                await handle_device_session(ws, parsed)
                return
        # Not a device hello: process this first message, then continue the
        # existing browser loop.
        await _process_browser_message(ws, first)
        while True:
            message = await ws.receive()
            if message.get("type") != "websocket.receive":
                if message.get("type") == "websocket.disconnect":
                    break
                continue
            await _process_browser_message(ws, message)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
```

Note: `handle_device_session` calls `ws.accept()`? NO — accept happens here in `websocket_endpoint` before peeking. `handle_device_session` must NOT call accept again. (Task 3's handler does not call accept — confirm it doesn't.)

- [ ] **Step 4: Run the dispatch test**

Run: `python -m pytest tests/test_ws_dispatch.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: ALL PASS (opus, device_messages, device_protocol, ws_dispatch, plus all pre-existing tests)

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_ws_dispatch.py
git commit -m "feat: route /ws to device handler on hello, keep browser protocol"
```

---

### Task 5: Pin PyAV in requirements + device integration harness

**Files:**
- Modify: `requirements.txt`
- Create: `device_e2e_test.py`

**Interfaces:**
- Consumes: the running app on `ws://localhost:8090/ws`, `app.opus_decode._encode_pcm_to_opus`.
- Produces: a manual integration script that mimics the device: connect → send hello → expect hello reply → listen start → stream real Opus frames → listen stop → expect `line_art_transcription` + `line_art`. Not run in CI (needs live services).

- [ ] **Step 1: Add PyAV to `requirements.txt`**

PyAV is now a real runtime dependency (the device path imports `av`). Append this line to `requirements.txt`:

```
av>=12.0.0
```

(Keep all existing lines. Do not remove anything.)

- [ ] **Step 2: Verify the app still imports cleanly**

Run: `python -c "import app.main; print('import OK')"`
Expected: prints `import OK`

- [ ] **Step 3: Create `device_e2e_test.py`**

```python
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
```

- [ ] **Step 4: Commit**

```bash
git add requirements.txt device_e2e_test.py
git commit -m "chore: pin PyAV; add device-protocol integration harness"
```

---

### Task 6: Full verification + live device sanity

**Files:** none (verification only).

- [ ] **Step 1: Run the whole unit suite**

Run: `python -m pytest -q`
Expected: ALL PASS (opus_decode, device_messages, device_protocol, ws_dispatch, and every pre-existing test).

- [ ] **Step 2: Restart the app to load the new code**

```bash
# stop any uvicorn on 8090, then:
python -m uvicorn app.main:app --host 0.0.0.0 --port 8090
```
(Run in the background. Confirm the log line `Server ready (offline)...` appears.)

- [ ] **Step 3: Synthesize a speech WAV via Speaches TTS and run the device harness**

```bash
curl -s "http://localhost:8001/v1/audio/speech" -H "Content-Type: application/json" \
  -d '{"model":"speaches-ai/Kokoro-82M-v1.0-ONNX","voice":"af_heart","input":"a cat","response_format":"wav"}' \
  -o spoken.wav
python device_e2e_test.py spoken.wav
```
Expected: prints `<- hello ...`, `line_art_transcription`, `line_art`, then `PASS` with `raw_mono OK: <h*48> bytes`.

- [ ] **Step 4: Live device check**

Power on the Cheeko device (configured to `ws://192.168.0.181:8090/ws`). Trigger a voice session. Expected: device no longer logs `Failed to receive server hello`; it shows the transcription, then prints the line-art bitmap. Capture the device serial log to confirm `line_art` was received.

- [ ] **Step 5: Clean up transient artifacts (do not commit generated media)**

```bash
rm -f spoken.wav
```

- [ ] **Step 6: Final commit if anything else changed**

```bash
git add -A && git commit -m "chore: device protocol verified end-to-end" || echo "nothing to commit"
```

---

## Self-Review Notes

- **Spec coverage:** hello handshake (T2 builder + T3 handler + T4 route), Opus→WAV decode via PyAV no-Ogg (T1), line_art_* message shapes (T2) and ordering + watchdog-friendly progress (T3), listen start/stop end-of-utterance + close flush (T3), session_id echoed everywhere (T2/T3), browser protocol preserved + auto-detect (T4), no-TTS/no-MCP/no-RFID scope (omitted by construction), error paths stt/image_gen (T3), PyAV dependency pinned (T5), integration harness + live check (T5/T6). All spec sections mapped.
- **Placeholder scan:** none — every code/test step is complete.
- **Type consistency:** `decode_opus_to_wav(frames, sample_rate=16000)`, `_encode_pcm_to_opus(pcm, sample_rate, frame_samples)`, the five `device_messages` builders, and `handle_device_session(ws, first_message, *, transcribe, generate_line_art, decode)` are used identically across tasks and tests. `generate_line_art` returns the existing 4-tuple `(data_uri, prompt_used, raw_mono_b64, height)`; the handler uses index 2 (`raw_mono`) and 3 (`height`) with fixed `width=384`. `handle_device_session` does NOT call `ws.accept()` (the endpoint does) — noted in T4.
```
