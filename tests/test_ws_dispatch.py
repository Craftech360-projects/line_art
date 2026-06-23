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


def _bytes(b):
    return {"type": "websocket.receive", "bytes": b}


def _sent_types(ws):
    """Decode each captured send into its 'type' field (sends are JSON strings)."""
    out = []
    for s in ws.sent:
        out.append(json.loads(s)["type"])
    return out


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


@pytest.mark.asyncio
async def test_audio_waits_for_confirm_then_generates(monkeypatch):
    captured = {}

    async def fake_transcribe(audio):
        return "a cat"

    async def fake_generate(subject):
        captured["subject"] = subject
        return ("data:image/png;base64,AAA", f"prompt {subject}", "cmF3", 240)

    monkeypatch.setattr(main, "transcribe", fake_transcribe)
    monkeypatch.setattr(main, "generate_line_art", fake_generate)

    ws = FakeWS([_bytes(b"WAVDATA"), _text({"type": "print_confirm"})])
    await main.websocket_endpoint(ws)

    types = _sent_types(ws)
    assert "transcription" in types
    assert "result" in types
    # transcription is sent before any generating-progress or result
    assert types.index("transcription") < types.index("result")
    assert captured["subject"] == "a cat"


@pytest.mark.asyncio
async def test_audio_alone_does_not_generate(monkeypatch):
    async def fake_transcribe(audio):
        return "a cat"

    async def fake_generate(subject):
        raise AssertionError("generate must not run before print_confirm")

    monkeypatch.setattr(main, "transcribe", fake_transcribe)
    monkeypatch.setattr(main, "generate_line_art", fake_generate)

    ws = FakeWS([_bytes(b"WAVDATA")])  # no confirm
    await main.websocket_endpoint(ws)

    types = _sent_types(ws)
    assert "transcription" in types
    assert "result" not in types


@pytest.mark.asyncio
async def test_reject_sends_nothing_and_does_not_generate(monkeypatch):
    async def fake_transcribe(audio):
        return "a cat"

    async def fake_generate(subject):
        raise AssertionError("generate must not run on print_reject")

    monkeypatch.setattr(main, "transcribe", fake_transcribe)
    monkeypatch.setattr(main, "generate_line_art", fake_generate)

    ws = FakeWS([_bytes(b"WAVDATA"), _text({"type": "print_reject"})])
    await main.websocket_endpoint(ws)

    types = _sent_types(ws)
    assert "transcription" in types
    assert "result" not in types
    assert "error" not in types


@pytest.mark.asyncio
async def test_confirm_with_no_pending_is_ignored(monkeypatch):
    async def fake_generate(subject):
        raise AssertionError("generate must not run with no pending transcription")

    monkeypatch.setattr(main, "generate_line_art", fake_generate)

    ws = FakeWS([_text({"type": "print_confirm"})])
    await main.websocket_endpoint(ws)

    assert ws.sent == []  # nothing sent at all


@pytest.mark.asyncio
async def test_new_audio_voids_pending_then_confirm_uses_new_text(monkeypatch):
    texts = iter(["old fox", "new owl"])

    async def fake_transcribe(audio):
        return next(texts)

    seen = {}

    async def fake_generate(subject):
        seen["subject"] = subject
        return ("data:image/png;base64,AAA", "p", "cmF3", 240)

    monkeypatch.setattr(main, "transcribe", fake_transcribe)
    monkeypatch.setattr(main, "generate_line_art", fake_generate)

    ws = FakeWS([_bytes(b"one"), _bytes(b"two"), _text({"type": "print_confirm"})])
    await main.websocket_endpoint(ws)

    assert seen["subject"] == "new owl"


@pytest.mark.asyncio
async def test_typed_text_still_generates_immediately(monkeypatch):
    seen = {}

    async def fake_generate(subject):
        seen["subject"] = subject
        return ("data:image/png;base64,AAA", "p", "cmF3", 240)

    async def boom_transcribe(audio):
        raise AssertionError("transcribe must not run for typed text")

    monkeypatch.setattr(main, "generate_line_art", fake_generate)
    monkeypatch.setattr(main, "transcribe", boom_transcribe)

    ws = FakeWS([_text({"type": "text_input", "text": "a dog"})])
    await main.websocket_endpoint(ws)

    types = _sent_types(ws)
    assert "result" in types
    assert seen["subject"] == "a dog"


@pytest.mark.asyncio
async def test_empty_stt_sends_error_and_confirm_is_noop(monkeypatch):
    async def fake_transcribe(audio):
        return ""

    async def fake_generate(subject):
        raise AssertionError("generate must not run after empty STT")

    monkeypatch.setattr(main, "transcribe", fake_transcribe)
    monkeypatch.setattr(main, "generate_line_art", fake_generate)

    ws = FakeWS([_bytes(b"WAVDATA"), _text({"type": "print_confirm"})])
    await main.websocket_endpoint(ws)

    types = _sent_types(ws)
    assert "error" in types
    assert "result" not in types
