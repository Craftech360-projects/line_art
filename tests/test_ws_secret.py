import pytest
from app import device_protocol as dp
from app import config


class _FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = None

    async def send_json(self, msg):
        self.sent.append(msg)

    async def receive(self):
        return {"type": "websocket.disconnect"}

    async def close(self, code=1008):
        self.closed = code


@pytest.mark.asyncio
async def test_hello_rejected_on_bad_secret(monkeypatch):
    monkeypatch.setattr(config, "WS_SHARED_SECRET", "s3cret")
    ws = _FakeWS()
    await dp.handle_device_session(ws, {"type": "hello", "auth": "wrong"})
    assert ws.closed == 1008
    assert not ws.sent  # no hello_reply issued


@pytest.mark.asyncio
async def test_hello_accepted_with_good_secret(monkeypatch):
    monkeypatch.setattr(config, "WS_SHARED_SECRET", "s3cret")
    ws = _FakeWS()
    await dp.handle_device_session(ws, {"type": "hello", "auth": "s3cret"})
    assert ws.closed is None
    assert ws.sent and ws.sent[0].get("type") == "hello"  # hello_reply sent
