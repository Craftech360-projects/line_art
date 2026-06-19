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
