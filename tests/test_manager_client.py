import httpx
import pytest
from app import manager_client as mc
from app import config


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)


@pytest.fixture(autouse=True)
def _reset_cache_and_config(monkeypatch):
    mc._cache.update({"data": None, "ts": 0.0})
    monkeypatch.setattr(config, "MANAGER_API_BASE_URL", "http://mgr")
    monkeypatch.setattr(config, "SERVICE_SECRET_KEY", "svc")
    monkeypatch.setattr(config, "STT_PROVIDER_TTL_S", 300.0)


@pytest.mark.asyncio
async def test_fetches_and_maps_active_stt():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/providers/active"
        assert request.headers.get("x-service-key") == "svc"
        return httpx.Response(200, json={"data": {"stt": {
            "provider": "deepgram", "model": "nova-2", "language": "en", "api_key": "dk"}}})

    async with _client(handler) as c:
        cfg = await mc.get_active_stt(c, now=1000.0)
    assert (cfg.provider, cfg.model, cfg.api_key) == ("deepgram", "nova-2", "dk")


@pytest.mark.asyncio
async def test_uses_cache_within_ttl_without_refetch():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"stt": {
            "provider": "groq", "model": "m", "language": "", "api_key": "k"}})

    async with _client(handler) as c:
        await mc.get_active_stt(c, now=1000.0)
        await mc.get_active_stt(c, now=1100.0)  # within 300s TTL
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_serves_last_known_good_on_fetch_error():
    async with _client(lambda r: httpx.Response(200, json={"stt": {
            "provider": "groq", "model": "m", "language": "", "api_key": "k"}})) as c:
        first = await mc.get_active_stt(c, now=1000.0)
    # cache now populated; a later fetch that errors must return the cached cfg
    def boom(request):
        raise httpx.ConnectError("down", request=request)
    async with _client(boom) as c2:
        second = await mc.get_active_stt(c2, now=9999.0)  # past TTL -> refetch -> error
    assert second.provider == first.provider == "groq"


@pytest.mark.asyncio
async def test_returns_none_when_no_base_url(monkeypatch):
    monkeypatch.setattr(config, "MANAGER_API_BASE_URL", "")
    assert await mc.get_active_stt(now=1000.0) is None


@pytest.mark.asyncio
async def test_moderation_block_is_parsed():
    payload = {"data": {
        "stt": {"provider": "groq", "model": "whisper-large-v3", "api_key": "sk1"},
        "moderation": {"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk2"},
    }}
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    async with _client(handler) as c:
        cfg = await mc.get_active_moderation(client=c, now=1000.0)
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o-mini"
    assert cfg.api_key == "sk2"


@pytest.mark.asyncio
async def test_missing_moderation_block_returns_none():
    payload = {"data": {"stt": {"provider": "groq", "api_key": "sk1"}}}
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    async with _client(handler) as c:
        assert await mc.get_active_moderation(client=c, now=1000.0) is None


@pytest.mark.asyncio
async def test_stt_and_moderation_share_one_fetch_and_cache():
    calls = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": {
            "stt": {"provider": "groq", "api_key": "sk1"},
            "moderation": {"provider": "groq", "api_key": "sk1"},
        }})
    async with _client(handler) as c:
        await mc.get_active_stt(client=c, now=1000.0)
        await mc.get_active_moderation(client=c, now=1001.0)  # within TTL
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_fetch_failure_serves_last_known_good_moderation():
    ok = {"data": {"moderation": {"provider": "groq", "api_key": "sk1"}}}
    state = {"fail": False}
    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(500)
        return httpx.Response(200, json=ok)
    async with _client(handler) as c:
        first = await mc.get_active_moderation(client=c, now=1000.0)
        state["fail"] = True
        second = await mc.get_active_moderation(client=c, now=2000.0)  # past TTL
    assert first.provider == "groq"
    assert second is not None and second.provider == "groq"  # last-known-good


@pytest.mark.asyncio
async def test_malformed_200_does_not_clobber_last_known_good():
    ok = {"data": {"stt": {"provider": "groq", "api_key": "sk1"},
                   "moderation": {"provider": "groq", "api_key": "sk1"}}}
    state = {"garbage": False}
    def handler(request: httpx.Request) -> httpx.Response:
        if state["garbage"]:
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(200, json=ok)
    async with _client(handler) as c:
        first = await mc.get_active_stt(client=c, now=1000.0)
        state["garbage"] = True
        stt = await mc.get_active_stt(client=c, now=2000.0)      # past TTL
        mod = await mc.get_active_moderation(client=c, now=2000.0)
    assert first.provider == "groq"
    assert stt is not None and stt.provider == "groq"    # last-known-good preserved
    assert mod is not None and mod.provider == "groq"


@pytest.mark.asyncio
async def test_image_block_is_parsed():
    payload = {"data": {
        "stt": {"provider": "groq", "api_key": "sk1"},
        "image": {"provider": "runware", "model": "runware:400@4", "api_key": "rk"},
    }}
    async with _client(lambda r: httpx.Response(200, json=payload)) as c:
        cfg = await mc.get_active_image(client=c, now=1000.0)
    assert cfg.provider == "runware"
    assert cfg.model == "runware:400@4"
    assert cfg.api_key == "rk"


@pytest.mark.asyncio
async def test_missing_image_block_returns_none():
    payload = {"data": {"stt": {"provider": "groq", "api_key": "sk1"}}}
    async with _client(lambda r: httpx.Response(200, json=payload)) as c:
        assert await mc.get_active_image(client=c, now=1000.0) is None
