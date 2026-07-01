"""Local<->cloud provider switch tests (STT_BACKEND / IMAGE_BACKEND)."""
import pytest
from app import stt, image_gen, config


@pytest.mark.asyncio
async def test_stt_uses_speaches_when_backend_local(monkeypatch):
    monkeypatch.setattr(config, "STT_BACKEND", "local")
    async def fake_speaches(audio, client=None):
        return "from speaches"
    async def fake_groq(audio, client=None):
        raise AssertionError("Groq must not be called in local mode")
    monkeypatch.setattr(stt, "_transcribe_speaches", fake_speaches)
    monkeypatch.setattr(stt, "_transcribe_groq", fake_groq)
    assert await stt.transcribe(b"wav") == "from speaches"


@pytest.mark.asyncio
async def test_stt_uses_groq_when_backend_groq(monkeypatch):
    monkeypatch.setattr(config, "STT_BACKEND", "groq")
    async def fake_groq(audio, client=None):
        return "from groq"
    monkeypatch.setattr(stt, "_transcribe_groq", fake_groq)
    assert await stt.transcribe(b"wav") == "from groq"


@pytest.mark.asyncio
async def test_image_uses_comfyui_when_backend_comfyui(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_BACKEND", "comfyui")
    seen = {}
    async def fake_comfy(prompt, width=768, height=768):
        seen["wh"] = (width, height)
        return b"PNGBYTES"
    monkeypatch.setattr(image_gen.comfy_client, "generate_png", fake_comfy)
    out = await image_gen._generate_image_bytes("a cat", width=512, height=384)
    assert out == b"PNGBYTES"
    assert seen["wh"] == (512, 384)


@pytest.mark.asyncio
async def test_image_uses_hf_when_backend_hf(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_BACKEND", "hf")
    async def fake_hf(prompt, width=None, height=None):
        return b"HFBYTES"
    monkeypatch.setattr(image_gen, "generate_with_huggingface", fake_hf)
    assert await image_gen._generate_image_bytes("a cat", width=512, height=384) == b"HFBYTES"
