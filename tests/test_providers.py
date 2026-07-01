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
    async def fake_comfy(prompt, width=768, height=768, timeout_s=None):
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


@pytest.mark.asyncio
async def test_generation_failure_serves_fallback(monkeypatch, tmp_path):
    import io
    from PIL import Image
    fb = tmp_path / "fb.jpg"
    Image.new("RGB", (640, 480), (0, 128, 0)).save(str(fb), format="JPEG")
    monkeypatch.setattr(config, "IMAGINE_FALLBACK_IMAGE", str(fb))
    async def boom(prompt, width=None, height=None):
        raise RuntimeError("ComfyUI unavailable")
    async def safe(subject, client=None):
        return True, ""
    monkeypatch.setattr(image_gen, "_generate_image_bytes", boom)
    monkeypatch.setattr(image_gen.moderation, "is_prompt_safe", safe)
    jpeg, _prompt = await image_gen.generate_imagine_jpeg("a cat")
    assert Image.open(io.BytesIO(jpeg)).size == (320, 240)  # fallback normalized to device size


@pytest.mark.asyncio
async def test_safety_block_is_not_replaced_by_fallback(monkeypatch, tmp_path):
    from PIL import Image
    fb = tmp_path / "fb.jpg"
    Image.new("RGB", (320, 240)).save(str(fb), format="JPEG")
    monkeypatch.setattr(config, "IMAGINE_FALLBACK_IMAGE", str(fb))
    with pytest.raises(ValueError) as exc:
        await image_gen.generate_imagine_jpeg("a scary zombie covered in blood")
    assert "safety_block" in str(exc.value)  # unsafe prompt stays blocked, no fallback
