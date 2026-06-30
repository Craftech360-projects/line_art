import httpx
import pytest

from app import stt


def _mock_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, timeout=30.0)


@pytest.mark.asyncio
async def test_transcribe_posts_to_groq_and_returns_text(monkeypatch):
    monkeypatch.setattr(stt.config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(stt.config, "GROQ_MODEL", "whisper-large-v3")

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "  a cat  "})

    async with _mock_client(handler) as client:
        text = await stt.transcribe(b"RIFFfake-wav-bytes", client=client)

    assert text == "a cat"
    assert seen["url"] == stt.GROQ_API_URL
    assert seen["auth"] == "Bearer test-key"
    assert b"whisper-large-v3" in seen["body"]
    assert b"audio.wav" in seen["body"]


@pytest.mark.asyncio
async def test_transcribe_raises_when_api_key_missing(monkeypatch):
    monkeypatch.setattr(stt.config, "GROQ_API_KEY", None)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY not set"):
        await stt.transcribe(b"x")


@pytest.mark.asyncio
async def test_transcribe_raises_clear_error_when_service_down(monkeypatch):
    monkeypatch.setattr(stt.config, "GROQ_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _mock_client(handler) as client:
        with pytest.raises(RuntimeError, match="Groq unavailable"):
            await stt.transcribe(b"x", client=client)
