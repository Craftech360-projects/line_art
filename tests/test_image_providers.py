"""Image provider adapters + fallback chain."""
import base64
import json

import httpx
import pytest

from app import config, image_gen
from app.stt_providers import ProviderConfig

PNG1x1 = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    b"h6FO1AAAAABJRU5ErkJggg==")


@pytest.mark.asyncio
async def test_runware_adapter_posts_task_and_decodes_base64():
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        body = json.loads(request.content)
        seen["task"] = body[0]
        return httpx.Response(200, json={"data": [
            {"taskType": "imageInference",
             "imageBase64Data": base64.b64encode(PNG1x1).decode()}]})
    cfg = ProviderConfig("runware", "runware:400@4", "", "rk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await image_gen.generate_image_with(cfg, "a cat", 512, 384, client=c)
    assert out == PNG1x1
    assert seen["host"] == "api.runware.ai"
    assert seen["task"]["model"] == "runware:400@4"
    assert seen["task"]["positivePrompt"] == "a cat"
    assert (seen["task"]["width"], seen["task"]["height"]) == (512, 384)


@pytest.mark.asyncio
async def test_fal_adapter_posts_prompt_then_downloads_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fal.run":
            assert request.headers["Authorization"] == "Key fk"
            assert request.url.path == "/fal-ai/flux/schnell"
            return httpx.Response(200, json={"images": [{"url": "https://cdn.fal.example/x.png"}]})
        return httpx.Response(200, content=PNG1x1)
    cfg = ProviderConfig("fal", "fal-ai/flux/schnell", "", "fk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await image_gen.generate_image_with(cfg, "a cat", 512, 384, client=c)
    assert out == PNG1x1


@pytest.mark.asyncio
async def test_hf_adapter_uses_cfg_key_and_model():
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        return httpx.Response(200, content=PNG1x1)
    cfg = ProviderConfig("hf", "black-forest-labs/FLUX.1-schnell", "", "hk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await image_gen.generate_image_with(cfg, "a cat", 512, 384, client=c)
    assert out == PNG1x1
    assert seen["auth"] == "Bearer hk"
    assert seen["path"].endswith("black-forest-labs/FLUX.1-schnell")


@pytest.mark.asyncio
async def test_http_error_raises_image_gen_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    cfg = ProviderConfig("runware", "runware:400@4", "", "rk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(image_gen.ImageGenUnavailable):
            await image_gen.generate_image_with(cfg, "a cat", 512, 384, client=c)


@pytest.mark.asyncio
async def test_variant_provider_routes_by_base_name():
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        return httpx.Response(200, json={"data": [
            {"imageBase64Data": base64.b64encode(PNG1x1).decode()}]})
    cfg = ProviderConfig("runware_schnell", "runware:100@1", "", "rk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await image_gen.generate_image_with(cfg, "a cat", 512, 384, client=c)
    assert out == PNG1x1 and seen["host"] == "api.runware.ai"


@pytest.mark.asyncio
async def test_chain_uses_manager_active_then_env_last_resort(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_BACKEND", "hf")
    monkeypatch.setattr(config, "HF_API_TOKEN", "envtoken")
    async def active_runware(client=None, now=None):
        return ProviderConfig("runware", "runware:400@4", "", "rk-bad")
    monkeypatch.setattr(image_gen.manager_client, "get_active_image", active_runware)
    calls = []
    async def fake_gen(cfg, prompt, width=None, height=None, client=None):
        calls.append(cfg.provider)
        if cfg.provider == "runware":
            raise image_gen.ImageGenUnavailable("runware down")
        return PNG1x1
    monkeypatch.setattr(image_gen, "generate_image_with", fake_gen)
    out = await image_gen._generate_image_bytes("a cat", width=512, height=384)
    assert out == PNG1x1
    assert calls == ["runware", "hf"]


@pytest.mark.asyncio
async def test_chain_skips_keyless_active(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_BACKEND", "hf")
    monkeypatch.setattr(config, "HF_API_TOKEN", "envtoken")
    async def active_keyless(client=None, now=None):
        return ProviderConfig("hf", "black-forest-labs/FLUX.1-schnell", "", "")
    monkeypatch.setattr(image_gen.manager_client, "get_active_image", active_keyless)
    calls = []
    async def fake_gen(cfg, prompt, width=None, height=None, client=None):
        calls.append((cfg.provider, cfg.api_key))
        return PNG1x1
    monkeypatch.setattr(image_gen, "generate_image_with", fake_gen)
    await image_gen._generate_image_bytes("a cat")
    assert calls == [("hf", "envtoken")]  # keyless active skipped, env last resort used
