"""Local<->cloud provider switch tests (STT_BACKEND / IMAGE_BACKEND)."""
import pytest
from app import stt, image_gen, config


@pytest.mark.asyncio
async def test_last_resort_config_defaults_to_groq(monkeypatch):
    monkeypatch.setattr(config, "STT_LAST_RESORT_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    monkeypatch.setattr(config, "GROQ_MODEL", "whisper-large-v3")
    cfg = stt._last_resort_config()
    assert cfg.provider == "groq" and cfg.api_key == "gk"


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
