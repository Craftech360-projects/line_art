import base64
import io
import logging
import os
import re
import time
from pathlib import Path

from PIL import Image

from app import comfy_client

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = (
    "simple black and white line art drawing of {subject}, "
    "minimal style, clean lines, white background, no shading, outline only"
)

TARGET_WIDTH = 384

# 1-bit conversion uses a fixed brightness threshold (NOT dithering): every pixel
# darker than MONO_THRESHOLD becomes black, else white. Dithering scatters dots to
# fake gray and turns thin line art into broken/speckled lines; a threshold keeps
# the lines solid and clean. Lower = fewer/lighter lines, higher = more/bolder.
MONO_THRESHOLD = int(os.environ.get("MONO_THRESHOLD", "190"))

# Every generation also saves a copy on the server: the original full-colour
# FLUX PNG and the 1-bit mono PNG the device prints.
_IMAGE_DIR = Path("generated_images")


def _save_copies(subject: str, full_png: bytes, mono_png: bytes) -> None:
    try:
        _IMAGE_DIR.mkdir(exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "_", subject.strip().lower()).strip("_")[:40] or "image"
        # time.time() is fine here (runtime side effect, not a workflow script).
        stamp = int(time.time())
        (_IMAGE_DIR / f"{stamp}_{slug}.png").write_bytes(full_png)
        (_IMAGE_DIR / f"{stamp}_{slug}_mono.png").write_bytes(mono_png)
        logger.info("Saved generated images for %r -> %s/%d_%s(.png/_mono.png)",
                    subject, _IMAGE_DIR, stamp, slug)
    except Exception:
        logger.exception("Failed to save generated image copies")


def build_prompt(subject: str) -> str:
    return PROMPT_TEMPLATE.format(subject=subject.strip())


def to_raw_mono(image_bytes: bytes) -> tuple[bytes, bytes]:
    """Convert image to 384px wide 1-bit monochrome raw bitmap.

    Returns (png_bytes_for_preview, raw_mono_bytes).

    Raw format:
      - 1-bit: black=1, white=0
      - MSB first (leftmost pixel = bit 7)
      - Top-down row order
      - 48 bytes per row (384 / 8), no padding
      - No header, no compression
    """
    img = Image.open(io.BytesIO(image_bytes))

    # Resize to 384px wide, preserve aspect ratio
    w, h = img.size
    new_h = int(h * TARGET_WIDTH / w)
    img = img.resize((TARGET_WIDTH, new_h), Image.LANCZOS)

    # Convert to 1-bit monochrome via a fixed threshold (no dithering), so thin
    # line art stays solid instead of being broken into scattered dots. Grayscale
    # first, then map dark<->black / light<->white at MONO_THRESHOLD.
    gray = img.convert("L")
    img_mono = gray.point(lambda p: 255 if p >= MONO_THRESHOLD else 0).convert("1")

    # Generate PNG preview
    png_buf = io.BytesIO()
    img_mono.save(png_buf, format="PNG")
    png_bytes = png_buf.getvalue()

    # Pack into raw bytes: black=1, white=0, MSB first
    pixels = img_mono.load()
    raw = bytearray()
    for y in range(new_h):
        for x_byte in range(TARGET_WIDTH // 8):
            byte = 0
            for bit in range(8):
                x = x_byte * 8 + bit
                # Pillow "1" mode: 0 = black, 255 = white
                # Firmware wants: 1 = black, 0 = white
                if pixels[x, y] == 0:
                    byte |= (0x80 >> bit)
            raw.append(byte)

    return png_bytes, bytes(raw)


async def generate_line_art(subject: str) -> tuple[str, str, str, int]:
    """Generate line art via local ComfyUI.

    Returns (base64_image_uri, prompt_used, base64_raw_mono, height).
    """
    prompt = build_prompt(subject)
    image_bytes = await comfy_client.generate_png(prompt)

    png_bytes, raw_bytes = to_raw_mono(image_bytes)
    _save_copies(subject, image_bytes, png_bytes)
    image_b64 = base64.b64encode(png_bytes).decode()
    raw_b64 = base64.b64encode(raw_bytes).decode()
    height = len(raw_bytes) // 48  # 48 bytes per row

    return f"data:image/png;base64,{image_b64}", prompt, raw_b64, height
