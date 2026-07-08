"""Moderation provider adapters + fallback chain."""
import httpx
import pytest

from app import config, moderation
from app.stt_providers import ProviderConfig


def _chat_ok(verdict: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": verdict}}]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "MODERATION_BACKEND", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    monkeypatch.setattr(config, "GROQ_LLM_MODEL", "llama-3.1-8b-instant")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,url_host", [
    ("groq", "api.groq.com"),
    ("openai", "api.openai.com"),
    ("openrouter", "openrouter.ai"),
])
async def test_chat_adapter_hits_right_host_and_parses_safe(provider, url_host):
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "SAFE"}}]})
    cfg = ProviderConfig(provider, "some-model", "", "key123")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        safe, reason = await moderation.check_with(cfg, "a happy puppy", c)
    assert safe is True and reason == ""
    assert seen["host"] == url_host
    assert seen["auth"] == "Bearer key123"


@pytest.mark.asyncio
async def test_chat_adapter_unsafe_verdict_blocks():
    cfg = ProviderConfig("groq", "llama-3.1-8b-instant", "", "gk")
    async with _chat_ok("UNSAFE") as c:
        safe, reason = await moderation.check_with(cfg, "something bad", c)
    assert safe is False and reason


@pytest.mark.asyncio
async def test_openai_moderation_adapter_parses_flagged():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/moderations"
        return httpx.Response(200, json={"results": [{"flagged": True}]})
    cfg = ProviderConfig("openai_moderation", "omni-moderation-latest", "", "sk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        safe, reason = await moderation.check_with(cfg, "something", c)
    assert safe is False


@pytest.mark.asyncio
async def test_http_error_raises_moderation_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    cfg = ProviderConfig("groq", "llama-3.1-8b-instant", "", "gk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(moderation.ModerationUnavailable):
            await moderation.check_with(cfg, "anything", c)


@pytest.mark.asyncio
async def test_chain_falls_back_to_last_resort_on_active_failure(monkeypatch):
    async def active_openai(client=None, now=None):
        return ProviderConfig("openai", "gpt-4o-mini", "", "sk-bad")
    monkeypatch.setattr(moderation.manager_client, "get_active_moderation", active_openai)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openai.com":
            return httpx.Response(500)  # active provider hard-fails
        return httpx.Response(200, json={"choices": [{"message": {"content": "UNSAFE"}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        safe, reason = await moderation.is_prompt_safe("something", client=c)
    assert safe is False  # groq last-resort answered, not fail-open


@pytest.mark.asyncio
async def test_all_providers_down_fails_open(monkeypatch):
    async def no_active(client=None, now=None):
        return None
    monkeypatch.setattr(moderation.manager_client, "get_active_moderation", no_active)
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        safe, reason = await moderation.is_prompt_safe("anything", client=c)
    assert safe is True  # fail-open preserved


@pytest.mark.asyncio
async def test_moderation_off_skips_everything(monkeypatch):
    monkeypatch.setattr(config, "MODERATION_BACKEND", "off")
    safe, _ = await moderation.is_prompt_safe("anything")
    assert safe is True


@pytest.mark.asyncio
async def test_chat_adapter_defaults_model_when_empty():
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen["model"] = _json.loads(request.content)["model"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "SAFE"}}]})
    cfg = ProviderConfig("openai", "", "", "sk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        safe, _ = await moderation.check_with(cfg, "a happy puppy", c)
    assert safe is True
    assert seen["model"] == "gpt-4o-mini"
