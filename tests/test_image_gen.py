import base64
import io

import pytest
from PIL import Image

from app import image_gen


def _png(color):
    buf = io.BytesIO()
    Image.new("RGB", (384, 8), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_generate_line_art_uses_comfy_and_returns_tuple(monkeypatch):
    async def fake_generate_png(prompt):
        assert "line art" in prompt  # prompt template applied
        return _png((0, 0, 0))  # all black

    monkeypatch.setattr(image_gen.comfy_client, "generate_png", fake_generate_png)

    data_uri, prompt_used, raw_b64, height = await image_gen.generate_line_art("a cat")

    assert data_uri.startswith("data:image/png;base64,")
    assert "a cat" in prompt_used
    raw = base64.b64decode(raw_b64)
    assert height == 8
    assert len(raw) == 8 * 48
    assert all(b == 0xFF for b in raw)  # black=1


def test_generate_line_art_has_no_hf_token_param():
    import inspect
    sig = inspect.signature(image_gen.generate_line_art)
    assert "hf_token" not in sig.parameters
    assert "api_token" not in sig.parameters
