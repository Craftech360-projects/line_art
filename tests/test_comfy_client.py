import httpx
import pytest

from app import comfy_client


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)


@pytest.mark.asyncio
async def test_generate_png_submits_polls_and_fetches(monkeypatch):
    monkeypatch.setattr(comfy_client.config, "COMFYUI_BASE_URL", "http://localhost:8188")
    state = {"history_calls": 0}
    PNG = b"\x89PNG\r\n\x1a\nfake"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "abc"})
        if path == "/history/abc":
            state["history_calls"] += 1
            if state["history_calls"] < 2:
                return httpx.Response(200, json={})  # not ready yet
            return httpx.Response(200, json={
                "abc": {"outputs": {"save": {"images": [
                    {"filename": "lineart_0001.png", "subfolder": "", "type": "output"}
                ]}}}
            })
        if path == "/view":
            assert request.url.params["filename"] == "lineart_0001.png"
            return httpx.Response(200, content=PNG)
        return httpx.Response(404)

    async def fake_sleep(_):
        return None

    async with _client(handler) as client:
        out = await comfy_client.generate_png(
            "a cat", client=client, sleep=fake_sleep, poll_interval=0.0
        )
    assert out == PNG
    assert state["history_calls"] >= 2


@pytest.mark.asyncio
async def test_generate_png_raises_when_service_down(monkeypatch):
    monkeypatch.setattr(comfy_client.config, "COMFYUI_BASE_URL", "http://localhost:8188")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with _client(handler) as client:
        with pytest.raises(RuntimeError, match="ComfyUI unavailable"):
            await comfy_client.generate_png("x", client=client)


@pytest.mark.asyncio
async def test_generate_png_times_out(monkeypatch):
    monkeypatch.setattr(comfy_client.config, "COMFYUI_BASE_URL", "http://localhost:8188")
    clock = {"t": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "abc"})
        return httpx.Response(200, json={})  # history never ready

    def fake_now():
        clock["t"] += 50.0  # advance fast past timeout
        return clock["t"]

    async def fake_sleep(_):
        return None

    async with _client(handler) as client:
        with pytest.raises(RuntimeError, match="timed out"):
            await comfy_client.generate_png(
                "x", client=client, now=fake_now, sleep=fake_sleep, timeout_s=120.0
            )
