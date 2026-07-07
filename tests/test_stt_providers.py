import httpx
import pytest
from app import stt_providers as sp
from app.stt_providers import ProviderConfig, STTHardFailure


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0)


@pytest.mark.asyncio
async def test_groq_adapter_returns_stripped_text():
    cfg = ProviderConfig(provider="groq", model="whisper-large-v3", language="", api_key="k")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer k"
        assert b"whisper-large-v3" in request.content
        return httpx.Response(200, json={"text": "  a cat  "})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "a cat"


@pytest.mark.asyncio
async def test_groq_adapter_empty_text_is_not_failure():
    cfg = ProviderConfig(provider="groq", model="m", language="", api_key="k")

    def handler(request):
        return httpx.Response(200, json={"text": "   "})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == ""


@pytest.mark.asyncio
async def test_groq_adapter_429_is_hard_failure():
    cfg = ProviderConfig(provider="groq", model="m", language="", api_key="k")

    def handler(request):
        return httpx.Response(429, json={"error": "rate limited"})

    async with _client(handler) as c:
        with pytest.raises(STTHardFailure):
            await sp.transcribe_with(cfg, b"wav", c)


@pytest.mark.asyncio
async def test_connect_error_is_hard_failure():
    cfg = ProviderConfig(provider="groq", model="m", language="", api_key="k")

    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    async with _client(handler) as c:
        with pytest.raises(STTHardFailure):
            await sp.transcribe_with(cfg, b"wav", c)


@pytest.mark.asyncio
async def test_unknown_provider_is_hard_failure():
    cfg = ProviderConfig(provider="nope", model="m", language="", api_key="k")
    with pytest.raises(STTHardFailure, match="no adapter"):
        await sp.transcribe_with(cfg, b"wav")


@pytest.mark.asyncio
async def test_deepgram_adapter_parses_transcript():
    cfg = ProviderConfig(provider="deepgram", model="nova-2", language="en", api_key="dk")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Token dk"
        assert "model=nova-2" in str(request.url)
        assert "language=en" in str(request.url)
        return httpx.Response(200, json={
            "results": {"channels": [{"alternatives": [{"transcript": "  a dog  "}]}]}
        })

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "a dog"


@pytest.mark.asyncio
async def test_deepgram_5xx_is_hard_failure():
    cfg = ProviderConfig(provider="deepgram", model="nova-2", language="", api_key="dk")

    def handler(request):
        return httpx.Response(503, text="unavailable")

    async with _client(handler) as c:
        with pytest.raises(STTHardFailure):
            await sp.transcribe_with(cfg, b"wav", c)


@pytest.mark.asyncio
async def test_deepgram_empty_channels_returns_empty():
    cfg = ProviderConfig(provider="deepgram", model="nova-2", language="", api_key="dk")

    def handler(request):
        return httpx.Response(200, json={"results": {"channels": []}})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == ""
