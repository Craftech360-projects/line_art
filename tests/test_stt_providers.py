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
async def test_groq_adapter_sends_language_when_set():
    """Without an explicit language Whisper auto-detects, and mis-detects short
    English clips as Hindi (transcribing them as Devanagari hallucinations)."""
    cfg = ProviderConfig(provider="groq", model="m", language="en", api_key="k")

    def handler(request: httpx.Request) -> httpx.Response:
        assert b'name="language"' in request.content
        assert b"en" in request.content
        return httpx.Response(200, json={"text": "a cat"})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "a cat"


@pytest.mark.asyncio
async def test_groq_adapter_omits_language_when_empty():
    """Empty language => omit the field entirely, restoring auto-detect."""
    cfg = ProviderConfig(provider="groq", model="m", language="", api_key="k")

    def handler(request: httpx.Request) -> httpx.Response:
        assert b'name="language"' not in request.content
        return httpx.Response(200, json={"text": "a cat"})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "a cat"


@pytest.mark.asyncio
async def test_speaches_adapter_sends_language_when_set():
    cfg = ProviderConfig(provider="speaches", model="m", language="en",
                         api_key="http://localhost:8001")

    def handler(request: httpx.Request) -> httpx.Response:
        assert b'name="language"' in request.content
        return httpx.Response(200, json={"text": "a cat"})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "a cat"


@pytest.mark.asyncio
async def test_sarvam_default_model_is_not_deprecated():
    """saarika:v2 is retired; defaulting to it 4xxs every call and silently
    demotes Sarvam to the fallback chain."""
    cfg = ProviderConfig(provider="sarvam", model="", language="en", api_key="sk")

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"saarika:v2.5" in request.content
        return httpx.Response(200, json={"transcript": "a cat"})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "a cat"


@pytest.mark.asyncio
async def test_sarvam_maps_iso_language_to_bcp47():
    """Sarvam wants BCP-47 with an Indian region (en-IN); a bare ISO-639-1 'en'
    is rejected, which would demote Sarvam to a hard failure on every call."""
    cfg = ProviderConfig(provider="sarvam", model="saarika:v2", language="en", api_key="sk")

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"en-IN" in request.content
        return httpx.Response(200, json={"transcript": "a cat"})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "a cat"


@pytest.mark.asyncio
async def test_sarvam_passes_through_regioned_code():
    cfg = ProviderConfig(provider="sarvam", model="saarika:v2", language="hi-IN", api_key="sk")

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"hi-IN" in request.content
        return httpx.Response(200, json={"transcript": "a cat"})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "a cat"


@pytest.mark.asyncio
async def test_sarvam_unsupported_language_falls_back_to_unknown():
    """A language Sarvam can't serve must auto-detect, not 400 the request."""
    cfg = ProviderConfig(provider="sarvam", model="saarika:v2", language="fr", api_key="sk")

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"unknown" in request.content
        assert b"fr-IN" not in request.content
        return httpx.Response(200, json={"transcript": "a cat"})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "a cat"


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


@pytest.mark.asyncio
async def test_sarvam_saaras_uses_translate_endpoint():
    cfg = ProviderConfig(provider="sarvam", model="saaras:v3", language="", api_key="sk")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/speech-to-text-translate"
        assert request.headers.get("api-subscription-key") == "sk"
        return httpx.Response(200, json={"transcript": " ek billi "})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "ek billi"


@pytest.mark.asyncio
async def test_sarvam_saarika_uses_stt_endpoint_with_language():
    cfg = ProviderConfig(provider="sarvam", model="saarika:v2", language="hi-IN", api_key="sk")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, json={"transcript": "namaste"})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "namaste"
    assert seen["path"] == "/speech-to-text"
    assert b"hi-IN" in seen["body"]
