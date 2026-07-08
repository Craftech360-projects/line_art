import asyncio
import base64
import io
import logging
import os
import random
import re
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image, ImageOps

from app import config
from app import moderation
from app import comfy_client
from app import manager_client
from app.stt_providers import ProviderConfig

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

# The whole provider chain must resolve inside the gateway's 90 s window so the
# imagine path can still serve fallback.jpg in time. ponytail: single overall
# deadline instead of per-adapter budgets.
IMAGE_CHAIN_DEADLINE_S = float(os.environ.get("IMAGE_CHAIN_DEADLINE_S", "75"))

# Every generation also saves a copy on the server: the original full-colour
# FLUX PNG and the 1-bit mono PNG the device prints.
_IMAGE_DIR = Path("generated_images")


def _save_copies(subject: str, full_png: bytes, mono_png: bytes) -> None:
    if not config.SAVE_GENERATED_IMAGES:
        return
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


async def generate_with_huggingface(prompt: str, width: int | None = None,
                                    height: int | None = None) -> bytes:
    """Generate a PNG from a prompt via the HuggingFace FLUX inference API.

    Pass width/height to request a specific aspect ratio (used by AI Imagine to get a
    4:3 image that fills the 320x240 LCD). Omit them for the printer path (unchanged).
    """
    headers = {}
    if config.HF_API_TOKEN:
        headers["Authorization"] = f"Bearer {config.HF_API_TOKEN}"
    payload = {"inputs": prompt}
    if width and height:
        payload["parameters"] = {"width": width, "height": height}
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(config.HF_MODEL_URL, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.content


RUNWARE_URL = "https://api.runware.ai/v1"


class ImageGenUnavailable(Exception):
    """Provider failure that should advance the image fallback chain."""


async def _gen_hf(cfg, prompt, width=None, height=None, client=None):
    model = cfg.model or ""
    url = model if model.startswith("http") else (
        f"https://router.huggingface.co/hf-inference/models/{model}" if model
        else config.HF_MODEL_URL)
    payload = {"inputs": prompt}
    if width and height:
        payload["parameters"] = {"width": width, "height": height}
    resp = await client.post(url, headers={"Authorization": f"Bearer {cfg.api_key}"},
                             json=payload)
    if resp.status_code // 100 != 2:
        raise ImageGenUnavailable(f"hf HTTP {resp.status_code}")
    return resp.content


async def _gen_runware(cfg, prompt, width=None, height=None, client=None):
    task = {
        "taskType": "imageInference",
        "taskUUID": str(uuid.uuid4()),
        "model": cfg.model or "runware:400@4",
        "positivePrompt": prompt,
        "width": width or 512,
        "height": height or 512,
        "steps": 4,
        "numberResults": 1,
        "outputType": "base64Data",
        "outputFormat": "PNG",
        "deliveryMethod": "sync",
    }
    resp = await client.post(RUNWARE_URL,
                             headers={"Authorization": f"Bearer {cfg.api_key}"},
                             json=[task])
    if resp.status_code // 100 != 2:
        raise ImageGenUnavailable(f"runware HTTP {resp.status_code}")
    data = (resp.json().get("data") or [])
    if not data or not data[0].get("imageBase64Data"):
        raise ImageGenUnavailable(f"runware: no image in response ({resp.text[:200]})")
    return base64.b64decode(data[0]["imageBase64Data"])


async def _gen_fal(cfg, prompt, width=None, height=None, client=None):
    path = cfg.model or "fal-ai/flux/schnell"
    body = {"prompt": prompt}
    if width and height:
        body["image_size"] = {"width": width, "height": height}
    resp = await client.post(f"https://fal.run/{path}",
                             headers={"Authorization": f"Key {cfg.api_key}"},
                             json=body)
    if resp.status_code // 100 != 2:
        raise ImageGenUnavailable(f"fal HTTP {resp.status_code}")
    images = resp.json().get("images") or []
    if not images or not images[0].get("url"):
        raise ImageGenUnavailable("fal: no image url in response")
    img = await client.get(images[0]["url"])
    if img.status_code // 100 != 2:
        raise ImageGenUnavailable(f"fal image download HTTP {img.status_code}")
    return img.content


IMAGE_ADAPTERS = {"hf": _gen_hf, "runware": _gen_runware, "fal": _gen_fal}


async def generate_image_with(cfg: ProviderConfig, prompt: str,
                              width=None, height=None, client=None) -> bytes:
    adapter = IMAGE_ADAPTERS.get(cfg.provider)
    if adapter is None and "_" in cfg.provider:
        base = cfg.provider.split("_", 1)[0]
        adapter = IMAGE_ADAPTERS.get(base)
        if adapter is not None:
            cfg = ProviderConfig(base, cfg.model, cfg.language, cfg.api_key)
    if adapter is None:
        raise ImageGenUnavailable(f"no adapter for image provider {cfg.provider!r}")
    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=120.0)
    try:
        return await adapter(cfg, prompt, width=width, height=height, client=client)
    except ImageGenUnavailable:
        raise
    except Exception as e:  # transport errors, bad JSON shape
        raise ImageGenUnavailable(f"{cfg.provider}: {e}") from e
    finally:
        if owns:
            await client.aclose()


def _image_last_resort() -> ProviderConfig | None:
    if not config.HF_API_TOKEN:
        return None
    return ProviderConfig("hf", "", "", config.HF_API_TOKEN)  # model "" -> config.HF_MODEL_URL


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


async def _generate_image_bytes(prompt: str, width: int | None = None,
                                height: int | None = None) -> bytes:
    """Generate raw image bytes: local ComfyUI override, else the provider chain
    (manager-api active image provider -> env HF last resort)."""
    if config.IMAGE_BACKEND == "comfyui":
        return await comfy_client.generate_png(
            prompt, width=width or 768, height=height or 768,
            timeout_s=config.COMFYUI_TIMEOUT_S)

    chain: list[ProviderConfig] = []
    active = await manager_client.get_active_image()
    if active is not None and active.api_key:
        chain.append(active)
    last = _image_last_resort()
    if last is not None and (not chain or chain[0].provider != last.provider):
        chain.append(last)  # depth <= 2
    if not chain:
        # No manager row with a key and no env token: legacy direct HF call
        # (works for public models without auth).
        if width and height:
            return await generate_with_huggingface(prompt, width=width, height=height)
        return await generate_with_huggingface(prompt)

    last_exc: Exception | None = None
    try:
        async with asyncio.timeout(IMAGE_CHAIN_DEADLINE_S):
            for cfg in chain:
                try:
                    return await generate_image_with(cfg, prompt, width=width, height=height)
                except ImageGenUnavailable as e:
                    last_exc = e
                    logger.warning("Image provider %s unavailable: %s", cfg.provider, e)
    except TimeoutError:
        raise RuntimeError(
            f"Image chain deadline ({IMAGE_CHAIN_DEADLINE_S:.0f}s) exceeded; last error: {last_exc}")
    raise RuntimeError(f"All image providers failed: {last_exc}")


async def generate_line_art(subject: str) -> tuple[str, str, str, int]:
    """Generate line art via the configured image backend.

    Returns (base64_image_uri, prompt_used, base64_raw_mono, height).
    """
    prompt = build_prompt(subject)
    image_bytes = await _generate_image_bytes(prompt)

    png_bytes, raw_bytes = to_raw_mono(image_bytes)
    _save_copies(subject, image_bytes, png_bytes)
    image_b64 = base64.b64encode(png_bytes).decode()
    raw_b64 = base64.b64encode(raw_bytes).decode()
    height = len(raw_bytes) // 48  # 48 bytes per row

    return f"data:image/png;base64,{image_b64}", prompt, raw_b64, height


# --- AI Imagine: color JPEG path (separate from the 1-bit printer path) ---

DEVICE_W, DEVICE_H = 320, 240
MAX_JPEG_BYTES = 200 * 1024

# One of these child-friendly art themes is picked at random per generation,
# so repeated prompts don't all come out in the same look.
IMAGINE_THEMES = [
    "bright cartoon style, simple shapes, clean plain background",
    "cute claymation style, colorful plasticine clay figures, soft 3D look, plain background",
    "cute kawaii style, rounded chubby shapes, big friendly eyes, pastel plain background",
    "colorful paper-cutout collage style, bold simple shapes, plain background",
    "cute 3D animated movie style, soft rounded characters, glossy colorful render, plain background",
    "soft felt plush toy style, fuzzy fabric texture, stitched details, plain background",
    "colorful pixel art style, chunky retro video game sprites, plain background",
    "child's crayon drawing style, waxy bright strokes, drawn on white paper",
]

IMAGINE_PROMPT_TEMPLATE = (
    "a colorful, friendly children's illustration of {subject}, "
    "{theme}, cheerful, safe for kids, "
    "no text, no words, no letters, no captions, no writing, no signature"
)

# Children speak in full sentences ("can you draw a beautiful cat"). Feeding the whole
# utterance to FLUX makes it render those words into the picture, so strip the leading
# request phrasing down to the actual subject ("a beautiful cat").
_SUBJECT_PREFIXES = [
    "hello", "hi", "hey", "okay", "ok",
    "can you please", "can you", "could you", "would you", "will you",
    "please draw me", "please draw", "please make", "please show me", "please",
    "i want you to draw", "i want a picture of", "i want to see", "i want",
    "i would like", "i'd like", "draw me a picture of", "draw me", "draw a picture of",
    "draw", "make me", "make a picture of", "make", "show me a picture of",
    "show me", "create a picture of", "create", "generate", "paint", "a picture of",
    "picture of", "a image of", "an image of", "image of",
]


def _clean_subject(subject: str) -> str:
    """Strip leading request phrasing so only the subject remains."""
    s = subject.strip().strip("?.!").strip()
    low = s.lower()
    changed = True
    while changed:
        changed = False
        for pref in _SUBJECT_PREFIXES:
            # match "hello can you" and "hello, can you" alike
            if low.startswith(pref + " ") or low.startswith(pref + ","):
                s = s[len(pref) + 1:].lstrip(" ,").strip()
                low = s.lower()
                changed = True
                break
    return s or subject.strip()


# Child-safety guard: block obviously unsafe subjects at the prompt boundary. This is a
# FIRST-LAYER keyword filter, not a full moderation model — it pairs with the kid-safe
# prompt template above. Raising here makes the gateway emit image_error code=safety_block.
_UNSAFE_TERMS = {
    # violence / weapons
    "gun", "guns", "rifle", "pistol", "knife", "knives", "weapon", "weapons", "bomb",
    "blood", "bloody", "gore", "gory", "kill", "killing", "murder", "dead", "death",
    "corpse", "fight", "fighting", "war", "shoot", "shooting", "stab", "behead", "violence",
    # scary / horror
    "horror", "scary", "creepy", "zombie", "demon", "devil", "satan", "ghost", "nightmare",
    "monster", "evil", "hell",
    # adult / sexual
    "nude", "naked", "nsfw", "sex", "sexy", "sexual", "porn", "boobs", "breast", "penis",
    "vagina", "butt", "lingerie",
    # substances
    "drug", "drugs", "alcohol", "beer", "wine", "vodka", "whiskey", "cigarette", "smoking",
    "weed", "cocaine",
    # self-harm / hate
    "suicide", "noose", "nazi", "swastika", "terrorist", "isis",
}


def _assert_child_safe(subject: str) -> None:
    """Raise on obviously unsafe subjects so the gateway returns a safety_block error."""
    words = set(re.findall(r"[a-z']+", subject.lower()))
    hits = words & _UNSAFE_TERMS
    if hits:
        raise ValueError(
            f"safety_block: subject not allowed for children ({', '.join(sorted(hits))})")


_last_theme = None


def _pick_theme() -> str:
    """Random theme, but never the same one twice in a row."""
    global _last_theme
    theme = random.choice([t for t in IMAGINE_THEMES if t != _last_theme])
    _last_theme = theme
    return theme


def build_imagine_prompt(subject: str) -> str:
    cleaned = _clean_subject(subject)
    _assert_child_safe(cleaned)
    theme = _pick_theme()
    logger.info("[imagine] theme picked: %r", theme)
    return IMAGINE_PROMPT_TEMPLATE.format(subject=cleaned, theme=theme)


def to_device_jpeg(image_bytes: bytes) -> bytes:
    """Fit into 320x240 WITHOUT cropping (letterbox), return baseline JPEG <= 200 KB (RGB).

    Cropping cut the subject's edges off; instead we scale-to-fit and pad so the whole
    picture is visible. A 4:3 source (see generate_imagine_jpeg) fills the screen with
    no visible bars; other aspects get small white margins rather than lost content.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    fitted = ImageOps.contain(img, (DEVICE_W, DEVICE_H), Image.LANCZOS)
    img = Image.new("RGB", (DEVICE_W, DEVICE_H), (255, 255, 255))
    img.paste(fitted, ((DEVICE_W - fitted.width) // 2, (DEVICE_H - fitted.height) // 2))

    data = b""
    for quality in (85, 75, 65, 55, 45, 35):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=False)
        data = buf.getvalue()
        if len(data) <= MAX_JPEG_BYTES:
            return data
    return data  # ponytail: accept the smallest attempt; 320x240 JPEG ~never exceeds 200KB


async def generate_imagine_jpeg(subject: str) -> tuple[bytes, str]:
    """Generate a color device JPEG for an imagine prompt. Returns (jpeg_bytes, prompt)."""
    prompt = build_imagine_prompt(subject)  # keyword safety pass (may raise safety_block)
    logger.info("[imagine] subject=%r -> prompt=%r", subject, prompt)
    safe, reason = await moderation.is_prompt_safe(subject)  # LLM safety pass (multilingual)
    logger.info("[imagine] moderation verdict: safe=%s reason=%r", safe, reason)
    if not safe:
        raise ValueError(f"safety_block: {reason}")
    # 4:3 landscape matches the 320x240 LCD (fills screen, no crop). 512x384 keeps FLUX
    # fast enough for the device's response window while staying sharp after downscale.
    t0 = time.time()
    try:
        image_bytes = await _generate_image_bytes(prompt, width=512, height=384)
        logger.info("[imagine] backend=%s returned %d bytes in %.1fs",
                    config.IMAGE_BACKEND, len(image_bytes), time.time() - t0)
    except Exception as e:
        # Generation backend failed (e.g. ComfyUI/HF unreachable). Serve the fallback
        # image so the device still shows something. (Safety blocks are raised above and
        # never reach here, so unsafe prompts are never replaced by the fallback.)
        fallback = config.IMAGINE_FALLBACK_IMAGE
        if fallback and os.path.exists(fallback):
            logger.warning("Imagine generation failed (%s); serving fallback %s", e, fallback)
            with open(fallback, "rb") as fh:
                image_bytes = fh.read()
        else:
            raise
    jpeg = to_device_jpeg(image_bytes)
    logger.info("[imagine] device JPEG ready: %d bytes (%dx%d letterboxed)",
                len(jpeg), DEVICE_W, DEVICE_H)
    return jpeg, prompt
