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
    err = next(m for m in ws.sent if m["type"] == "line_art_error")
    assert err["stage"] == "stt"


@pytest.mark.asyncio
async def test_disconnect_mid_listen_flushes():
    """Disconnect while listening (no listen stop) should flush buffered audio."""
    async def fake_transcribe(wav_bytes):
        return "a cat"

    async def fake_generate(subject):
        return ("data:image/png;base64,AAA", f"prompt {subject}", "cmF3", 240)

    def fake_decode(frames, sample_rate=16000):
        return b"RIFF"

    events = [
        _text({"type": "listen", "state": "start", "mode": "auto"}),
        _bytes(b"opus1"),
        _bytes(b"opus2"),
        {"type": "websocket.disconnect"},
    ]
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"},
        transcribe=fake_transcribe,
        generate_line_art=fake_generate,
        decode=fake_decode,
    )

    types = [m["type"] for m in ws.sent]
    assert "line_art" in types, f"expected line_art in {types}"


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
