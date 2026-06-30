import io
import pytest
from PIL import Image
from app import image_gen


def _solid_png(w: int, h: int, color=(10, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def test_to_device_jpeg_is_320x240_rgb_baseline_under_200k():
    # Oversized, wrong aspect source -> must be normalized to 320x240.
    data = image_gen.to_device_jpeg(_solid_png(1024, 1024))
    assert len(data) <= 200 * 1024
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    assert img.size == (320, 240)
    assert img.mode == "RGB"
    assert "progression" not in img.info  # baseline, not progressive


def test_to_device_jpeg_center_crops_to_4_3():
    # A very wide image should be cropped (not squished) to 4:3 then scaled.
    data = image_gen.to_device_jpeg(_solid_png(1200, 300))
    assert Image.open(io.BytesIO(data)).size == (320, 240)


def test_build_imagine_prompt_is_colorful_and_child_safe():
    p = image_gen.build_imagine_prompt("  a blue dog  ")
    assert "a blue dog" in p
    assert "children" in p.lower() or "cartoon" in p.lower()


@pytest.mark.asyncio
async def test_generate_imagine_jpeg_returns_jpeg_and_prompt(monkeypatch):
    async def fake_hf(prompt: str) -> bytes:
        return _solid_png(800, 600)
    monkeypatch.setattr(image_gen, "generate_with_huggingface", fake_hf)

    jpeg, prompt = await image_gen.generate_imagine_jpeg("a cat")
    assert Image.open(io.BytesIO(jpeg)).size == (320, 240)
    assert "a cat" in prompt
