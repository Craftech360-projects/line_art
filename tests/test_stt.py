import httpx
import pytest

from app import stt


def _mock_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, timeout=30.0)


@pytest.mark.asyncio
async def test_transcribe_posts_to_speaches_and_returns_text(monkeypatch):
    monkeypatch.setattr(stt.config, "SPEACHES_BASE_URL", "http://localhost:8001")
    monkeypatch.setattr(stt.config, "SPEACHES_MODEL", "Systran/faster-whisper-large-v3")

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "  a cat  "})

    async with _mock_client(handler) as client:
        text = await stt.transcribe(b"RIFFfake-wav-bytes", client=client)

    assert text == "a cat"
    assert seen["url"] == "http://localhost:8001/v1/audio/transcriptions"
    assert b"Systran/faster-whisper-large-v3" in seen["body"]
    assert b"audio.wav" in seen["body"]


@pytest.mark.asyncio
async def test_transcribe_raises_clear_error_when_service_down(monkeypatch):
    monkeypatch.setattr(stt.config, "SPEACHES_BASE_URL", "http://localhost:8001")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _mock_client(handler) as client:
        with pytest.raises(RuntimeError, match="Speaches unavailable"):
            await stt.transcribe(b"x", client=client)
