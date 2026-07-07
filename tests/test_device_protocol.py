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


def _confirm():
    return _text({"type": "print_confirm"})


def _reject():
    return _text({"type": "print_reject"})


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
async def test_empty_transcript_emits_error_not_line_art():
    async def empty_transcribe(wav):
        return "   "

    async def fake_generate(subject):  # should not be called
        raise AssertionError("generate must not run on empty transcript")

    events = [
        _text({"type": "listen", "state": "start"}),
        *[_bytes(b"x")] * 6,
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
async def test_disconnect_mid_listen_does_not_generate():
    """Disconnect while listening (no listen stop) must NOT run generation:
    the socket is gone, so an image could never be delivered. We skip the
    flush to avoid wasting a full (cold ~minutes) ComfyUI run."""
    async def fake_transcribe(wav_bytes):
        raise AssertionError("transcribe must not run after a device disconnect")

    async def fake_generate(subject):
        raise AssertionError("generate must not run after a device disconnect")

    def fake_decode(frames, sample_rate=16000):
        raise AssertionError("decode must not run after a device disconnect")

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

    # Only the hello reply should have been sent; no pipeline ran.
    types = [m["type"] for m in ws.sent]
    assert types == ["hello"], f"expected only hello, got {types}"


@pytest.mark.asyncio
async def test_generate_failure_emits_error():
    async def ok_transcribe(wav):
        return "a dog"

    async def boom_generate(subject):
        raise RuntimeError("ComfyUI unavailable")

    events = [
        _text({"type": "listen", "state": "start"}),
        *[_bytes(b"x")] * 6,
        _text({"type": "listen", "state": "stop"}),
        _text({"type": "print_confirm"}),
    ]
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"}, transcribe=ok_transcribe,
        generate_line_art=boom_generate, decode=lambda f, sample_rate=16000: b"RIFF",
    )
    err = next(m for m in ws.sent if m["type"] == "line_art_error")
    assert "ComfyUI unavailable" in err["message"]
    assert err["stage"] == "image_gen"


@pytest.mark.asyncio
async def test_transcription_waits_for_confirm_then_generates():
    captured = {}

    async def fake_transcribe(wav):
        return "a cat"

    async def fake_generate(subject):
        captured["subject"] = subject
        return ("data:image/png;base64,AAA", f"prompt {subject}", "cmF3bW9ubw==", 240)

    def fake_decode(frames, sample_rate=16000):
        return b"RIFF"

    events = [
        _text({"type": "listen", "state": "start"}),
        *[_bytes(b"op")] * 6,
        _text({"type": "listen", "state": "stop"}),
        _confirm(),
    ]
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"},
        transcribe=fake_transcribe, generate_line_art=fake_generate, decode=fake_decode,
    )
    types = [m["type"] for m in ws.sent]
    # transcription is sent and comes before any generation output
    assert "line_art_transcription" in types
    assert "line_art" in types
    assert types.index("line_art_transcription") < types.index("line_art_progress") < types.index("line_art")
    assert captured["subject"] == "a cat"
    final = next(m for m in ws.sent if m["type"] == "line_art")
    assert final["raw_mono"] == "cmF3bW9ubw==" and final["width"] == 384 and final["height"] == 240


@pytest.mark.asyncio
async def test_transcription_alone_does_not_generate():
    # listen-stop produces a transcription but NO confirm arrives -> no generation.
    async def fake_transcribe(wav):
        return "a cat"

    async def fake_generate(subject):
        raise AssertionError("generate must not run before print_confirm")

    events = [
        _text({"type": "listen", "state": "start"}),
        *[_bytes(b"op")] * 6,
        _text({"type": "listen", "state": "stop"}),
        # no confirm -> session ends
    ]
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"},
        transcribe=fake_transcribe, generate_line_art=fake_generate,
        decode=lambda f, sample_rate=16000: b"RIFF",
    )
    types = [m["type"] for m in ws.sent]
    assert "line_art_transcription" in types
    assert "line_art" not in types
    assert "line_art_progress" not in types


@pytest.mark.asyncio
async def test_reject_sends_nothing_and_does_not_generate():
    async def fake_transcribe(wav):
        return "a cat"

    async def fake_generate(subject):
        raise AssertionError("generate must not run on print_reject")

    events = [
        _text({"type": "listen", "state": "start"}),
        *[_bytes(b"op")] * 6,
        _text({"type": "listen", "state": "stop"}),
        _reject(),
    ]
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"},
        transcribe=fake_transcribe, generate_line_art=fake_generate,
        decode=lambda f, sample_rate=16000: b"RIFF",
    )
    types = [m["type"] for m in ws.sent]
    assert "line_art_transcription" in types
    assert "line_art" not in types
    assert "line_art_progress" not in types
    assert "line_art_error" not in types


@pytest.mark.asyncio
async def test_new_audio_voids_pending_then_confirm_uses_new_text():
    texts = iter(["old fox", "new owl"])

    async def fake_transcribe(wav):
        return next(texts)

    seen = {}

    async def fake_generate(subject):
        seen["subject"] = subject
        return ("data:image/png;base64,AAA", "p", "cmF3", 240)

    events = [
        _text({"type": "listen", "state": "start"}),   # first utterance
        *[_bytes(b"op")] * 6,
        _text({"type": "listen", "state": "stop"}),     # -> transcribe "old fox"
        _text({"type": "listen", "state": "start"}),     # NEW audio voids "old fox"
        *[_bytes(b"op2")] * 6,
        _text({"type": "listen", "state": "stop"}),      # -> transcribe "new owl"
        _confirm(),                                       # confirm -> generate "new owl"
    ]
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"},
        transcribe=fake_transcribe, generate_line_art=fake_generate,
        decode=lambda f, sample_rate=16000: b"RIFF",
    )
    assert seen["subject"] == "new owl"


@pytest.mark.asyncio
async def test_confirm_with_no_pending_is_ignored():
    async def fake_generate(subject):
        raise AssertionError("generate must not run with no pending transcription")

    events = [_confirm()]   # confirm with nothing pending
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"},
        transcribe=lambda w: "x", generate_line_art=fake_generate,
        decode=lambda f, sample_rate=16000: b"RIFF",
    )
    # only the hello reply was sent
    assert [m["type"] for m in ws.sent] == ["hello"]
