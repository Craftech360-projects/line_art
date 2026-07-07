import base64
import pytest
from app import device_protocol


class FakeWS:
    """Minimal Starlette-WebSocket double driven by a scripted inbound queue."""
    def __init__(self, inbound):
        self._inbound = list(inbound)
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)

    async def receive(self):
        if self._inbound:
            return self._inbound.pop(0)
        return {"type": "websocket.disconnect"}


@pytest.mark.asyncio
async def test_imagine_session_emits_image_without_print_confirm():
    inbound = [
        {"type": "websocket.receive", "text": '{"type":"listen","state":"start"}'},
        *[{"type": "websocket.receive", "bytes": b"\x01\x02"}] * 6,
        {"type": "websocket.receive", "text": '{"type":"listen","state":"stop"}'},
        {"type": "websocket.disconnect"},
    ]
    ws = FakeWS(inbound)

    async def fake_transcribe(wav):  # bypass STT
        return "a blue dog"

    def fake_decode(frames):
        return b"WAVDATA"

    async def fake_generate(subject):
        return b"JPEGBYTES", f"prompt::{subject}"

    await device_protocol.handle_device_session(
        ws,
        {"type": "hello", "feature": "ai_imagine"},
        transcribe=fake_transcribe,
        decode=fake_decode,
        generate_imagine=fake_generate,
    )

    types = [m["type"] for m in ws.sent]
    assert "line_art_transcription" in types
    assert "image" in types  # generated WITHOUT any print_confirm
    img = next(m for m in ws.sent if m["type"] == "image")
    assert base64.b64decode(img["image"]) == b"JPEGBYTES"
    assert img["caption"] == "a blue dog"
    assert "line_art" not in types  # printer message must NOT be emitted


@pytest.mark.asyncio
async def test_too_short_utterance_skips_stt_and_sends_error():
    # A knob-tap blip (1 frame) must NOT reach Whisper (it hallucinates "Thank you.")
    inbound = [
        {"type": "websocket.receive", "text": '{"type":"listen","state":"start"}'},
        {"type": "websocket.receive", "bytes": b"\x01"},
        {"type": "websocket.receive", "text": '{"type":"listen","state":"stop"}'},
        {"type": "websocket.disconnect"},
    ]
    ws = FakeWS(inbound)

    async def fake_transcribe(wav):
        raise AssertionError("STT must not run on a too-short utterance")

    async def fake_generate(subject):
        raise AssertionError("generation must not run on a too-short utterance")

    await device_protocol.handle_device_session(
        ws, {"type": "hello", "feature": "ai_imagine"},
        transcribe=fake_transcribe, decode=lambda f: b"WAV", generate_imagine=fake_generate,
    )
    types = [m["type"] for m in ws.sent]
    assert "line_art_error" in types
    assert "image" not in types


@pytest.mark.asyncio
async def test_chat_printer_path_unchanged_still_requires_confirm():
    # No feature flag -> classic path: transcription, then NOTHING until print_confirm.
    inbound = [
        {"type": "websocket.receive", "text": '{"type":"listen","state":"start"}'},
        *[{"type": "websocket.receive", "bytes": b"\x01"}] * 6,
        {"type": "websocket.receive", "text": '{"type":"listen","state":"stop"}'},
        {"type": "websocket.disconnect"},
    ]
    ws = FakeWS(inbound)

    async def fake_transcribe(wav):
        return "a cat"

    def fake_decode(frames):
        return b"WAV"

    async def fake_imagine(subject):
        raise AssertionError("imagine generator must not run on the printer path")

    await device_protocol.handle_device_session(
        ws, {"type": "hello"},
        transcribe=fake_transcribe, decode=fake_decode, generate_imagine=fake_imagine,
    )
    types = [m["type"] for m in ws.sent]
    assert "line_art_transcription" in types
    assert "image" not in types
    assert "line_art" not in types  # never confirmed
