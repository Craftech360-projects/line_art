# Offline Line Art Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the Line Art Generator to a fully offline server by replacing Groq (STT) with a local Speaches container and HuggingFace FLUX (image gen) with a local ComfyUI server.

**Architecture:** The FastAPI app keeps its WebSocket protocol and 1-bit bitmap output unchanged. `app/stt.py` calls a local Speaches OpenAI-compatible endpoint; `app/image_gen.py` drives a local ComfyUI HTTP API (`/prompt` → poll `/history` → fetch `/view`) and reuses the existing `to_raw_mono` packing. No cloud, no API keys.

**Tech Stack:** Python 3.11, FastAPI, Uvicorn, httpx (async), Pillow, Pydantic, pytest (with `httpx.MockTransport` for mocking), Docker (Speaches), native ComfyUI + FLUX.1-schnell fp8.

## Global Constraints

- App-side dependencies only: `fastapi`, `uvicorn[standard]`, `httpx`, `Pillow`, `pydantic`, `python-dotenv`. No `openai-whisper`, no HF SDK.
- No cloud fallback and no API keys anywhere. Services are local-only.
- Do NOT change the WebSocket protocol or the `raw_mono` bitmap format (384px wide, 48 bytes/row, MSB-first, black=1, white=0, top-down, no header).
- Config via env vars with defaults: `SPEACHES_BASE_URL=http://localhost:8001`, `SPEACHES_MODEL=Systran/faster-whisper-large-v3`, `COMFYUI_BASE_URL=http://localhost:8188`.
- Ports: FastAPI 8000, Speaches host 8001 (container 8000), ComfyUI 8188.
- The app must still boot even if Speaches/ComfyUI are down — services are checked per-request, not at startup.
- Tests use `httpx.MockTransport`; do NOT add new test dependencies.
- Mock external HTTP in unit tests — never hit a real Speaches/ComfyUI in tests.

---

### Task 1: Test scaffolding + config module

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `app/config.py`
- Create: `tests/test_config.py`
- Create: `pytest.ini`

**Interfaces:**
- Consumes: nothing.
- Produces: `app.config` module exposing module-level constants read from env at import time:
  - `SPEACHES_BASE_URL: str` (default `"http://localhost:8001"`)
  - `SPEACHES_MODEL: str` (default `"Systran/faster-whisper-large-v3"`)
  - `COMFYUI_BASE_URL: str` (default `"http://localhost:8188"`)
  - A helper `get(name: str, default: str) -> str` is NOT exposed; constants only.

- [ ] **Step 1: Create `pytest.ini`**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
asyncio_mode = auto
```

- [ ] **Step 2: Create empty `tests/__init__.py`**

```python
```

- [ ] **Step 3: Create `tests/conftest.py` so `app` is importable**

```python
import sys
from pathlib import Path

# Ensure project root is on sys.path so `import app...` works under pytest.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

- [ ] **Step 4: Write the failing test `tests/test_config.py`**

```python
import importlib
import os


def _reload_config():
    import app.config
    return importlib.reload(app.config)


def test_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("SPEACHES_BASE_URL", raising=False)
    monkeypatch.delenv("SPEACHES_MODEL", raising=False)
    monkeypatch.delenv("COMFYUI_BASE_URL", raising=False)
    cfg = _reload_config()
    assert cfg.SPEACHES_BASE_URL == "http://localhost:8001"
    assert cfg.SPEACHES_MODEL == "Systran/faster-whisper-large-v3"
    assert cfg.COMFYUI_BASE_URL == "http://localhost:8188"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("SPEACHES_BASE_URL", "http://host:9000")
    monkeypatch.setenv("COMFYUI_BASE_URL", "http://host:9188")
    cfg = _reload_config()
    assert cfg.SPEACHES_BASE_URL == "http://host:9000"
    assert cfg.COMFYUI_BASE_URL == "http://host:9188"
```

- [ ] **Step 5: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 6: Create `app/config.py`**

```python
import os

SPEACHES_BASE_URL = os.environ.get("SPEACHES_BASE_URL", "http://localhost:8001")
SPEACHES_MODEL = os.environ.get("SPEACHES_MODEL", "Systran/faster-whisper-large-v3")
COMFYUI_BASE_URL = os.environ.get("COMFYUI_BASE_URL", "http://localhost:8188")
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 8: Commit**

```bash
git add pytest.ini tests/__init__.py tests/conftest.py tests/test_config.py app/config.py
git commit -m "feat: add test scaffolding and local-services config module"
```

---

### Task 2: Speaches STT client (replaces Groq)

**Files:**
- Modify: `app/stt.py` (full rewrite)
- Create: `tests/test_stt.py`

**Interfaces:**
- Consumes: `app.config.SPEACHES_BASE_URL`, `app.config.SPEACHES_MODEL`.
- Produces: `async def transcribe(audio_bytes: bytes) -> str` — POSTs multipart to
  `{SPEACHES_BASE_URL}/v1/audio/transcriptions` (`file`=`("audio.wav", audio_bytes, "audio/wav")`,
  `model`=`SPEACHES_MODEL`, `response_format`=`"json"`), returns `result["text"].strip()`.
  Raises `RuntimeError("Speaches unavailable at <url>: <detail>")` on `httpx.ConnectError`/`ConnectTimeout`.
  Accepts optional `client: httpx.AsyncClient | None = None` param for test injection.

- [ ] **Step 1: Write the failing test `tests/test_stt.py`**

```python
import httpx
import pytest

from app import stt


def _mock_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, timeout=30.0)


@pytest.mark.asyncio
async def test_transcribe_posts_to_speaches_and_returns_text(monkeypatch):
    monkeypatch.setattr(stt.config, "SPEACHES_BASE_URL", "http://localhost:8001")
    monkeypatch.setattr(stt.config, "SPEACHES_MODEL", "Systran/faster-whisper-large-v3")

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "  a cat  "})

    async with _mock_client(handler) as client:
        text = await stt.transcribe(b"RIFFfake-wav-bytes", client=client)

    assert text == "a cat"
    assert seen["url"] == "http://localhost:8001/v1/audio/transcriptions"
    assert b"Systran/faster-whisper-large-v3" in seen["body"]
    assert b"audio.wav" in seen["body"]


@pytest.mark.asyncio
async def test_transcribe_raises_clear_error_when_service_down(monkeypatch):
    monkeypatch.setattr(stt.config, "SPEACHES_BASE_URL", "http://localhost:8001")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _mock_client(handler) as client:
        with pytest.raises(RuntimeError, match="Speaches unavailable"):
            await stt.transcribe(b"x", client=client)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stt.py -v`
Expected: FAIL (current `stt.py` references Groq, has no `config`, signature lacks `client`)

- [ ] **Step 3: Rewrite `app/stt.py`**

```python
import logging

import httpx

from app import config

logger = logging.getLogger(__name__)


async def transcribe(audio_bytes: bytes, client: httpx.AsyncClient | None = None) -> str:
    """Transcribe audio bytes to text using the local Speaches server."""
    url = f"{config.SPEACHES_BASE_URL}/v1/audio/transcriptions"
    files = {"file": ("audio.wav", audio_bytes, "audio/wav")}
    data = {"model": config.SPEACHES_MODEL, "response_format": "json"}

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.post(url, files=files, data=data)
        resp.raise_for_status()
        result = resp.json()
        text = result.get("text", "").strip()
        logger.info("Speaches transcription: '%s'", text)
        return text
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise RuntimeError(f"Speaches unavailable at {config.SPEACHES_BASE_URL}: {e}") from e
    finally:
        if owns_client:
            await client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_stt.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/stt.py tests/test_stt.py
git commit -m "feat: transcribe via local Speaches instead of Groq"
```

---

### Task 3: Lock the 1-bit conversion with a characterization test

This task adds a test for the EXISTING `to_raw_mono` so later refactors can't silently change device output. No production code changes.

**Files:**
- Create: `tests/test_image_packing.py`

**Interfaces:**
- Consumes: `app.image_gen.to_raw_mono(image_bytes: bytes) -> tuple[bytes, bytes]`.
- Produces: nothing (guard test only).

- [ ] **Step 1: Write the test `tests/test_image_packing.py`**

```python
import io

from PIL import Image

from app import image_gen


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_raw_mono_is_384_wide_48_bytes_per_row():
    # 768x512 white image -> resized to 384 wide, height 256
    img = Image.new("RGB", (768, 512), (255, 255, 255))
    _, raw = image_gen.to_raw_mono(_png_bytes(img))
    assert len(raw) == 256 * 48  # height 256, 48 bytes/row


def test_all_black_sets_all_bits_one():
    img = Image.new("RGB", (384, 8), (0, 0, 0))
    _, raw = image_gen.to_raw_mono(_png_bytes(img))
    assert len(raw) == 8 * 48
    assert all(b == 0xFF for b in raw)  # black=1, every bit set


def test_all_white_sets_all_bits_zero():
    img = Image.new("RGB", (384, 8), (255, 255, 255))
    _, raw = image_gen.to_raw_mono(_png_bytes(img))
    assert all(b == 0x00 for b in raw)  # white=0


def test_msb_first_left_pixel_is_high_bit():
    # Left half black, right half white on a single 384x1 row.
    img = Image.new("1", (384, 1), 1)  # all white in mode "1"
    for x in range(192):
        img.putpixel((x, 0), 0)  # left half black
    _, raw = image_gen.to_raw_mono(_png_bytes(img))
    assert raw[0] == 0xFF      # first 8 px black -> 11111111
    assert raw[24] == 0x00     # byte 24 covers px 192..199 -> white
```

- [ ] **Step 2: Run test to verify it passes (characterizes current behavior)**

Run: `python -m pytest tests/test_image_packing.py -v`
Expected: PASS (4 passed). If any fail, STOP — the current packing differs from the spec; report before continuing.

- [ ] **Step 3: Commit**

```bash
git add tests/test_image_packing.py
git commit -m "test: characterize 1-bit raw_mono packing as a regression guard"
```

---

### Task 4: ComfyUI workflow builder

**Files:**
- Create: `app/comfy_workflow.py`
- Create: `tests/test_comfy_workflow.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `build_flux_workflow(prompt: str, *, width: int = 768, height: int = 768, steps: int = 4, seed: int = 0) -> dict` — returns a ComfyUI prompt-graph dict for FLUX.1-schnell fp8. The graph contains a `CheckpointLoaderSimple` node loading `flux1-schnell-fp8.safetensors`, a positive `CLIPTextEncode` with the prompt, an `EmptyLatentImage` (width/height), a `KSampler` (steps, seed, sampler `euler`, scheduler `simple`, cfg 1.0), `VAEDecode`, and `SaveImage`. Node keys are stable strings; the `SaveImage` node has key `"save"`.

- [ ] **Step 1: Write the failing test `tests/test_comfy_workflow.py`**

```python
from app.comfy_workflow import build_flux_workflow


def test_workflow_embeds_prompt_and_dims():
    g = build_flux_workflow("simple line art of a cat", width=768, height=512, steps=4, seed=7)
    # Prompt appears in some CLIPTextEncode node.
    texts = [n["inputs"].get("text") for n in g.values() if n["class_type"] == "CLIPTextEncode"]
    assert "simple line art of a cat" in texts
    # Latent dims set.
    latents = [n for n in g.values() if n["class_type"] == "EmptyLatentImage"]
    assert latents and latents[0]["inputs"]["width"] == 768
    assert latents[0]["inputs"]["height"] == 512
    # Steps + seed on the sampler.
    samplers = [n for n in g.values() if n["class_type"] == "KSampler"]
    assert samplers and samplers[0]["inputs"]["steps"] == 4
    assert samplers[0]["inputs"]["seed"] == 7


def test_workflow_has_save_node_and_checkpoint():
    g = build_flux_workflow("x")
    assert g["save"]["class_type"] == "SaveImage"
    ckpts = [n for n in g.values() if n["class_type"] == "CheckpointLoaderSimple"]
    assert ckpts and "flux1-schnell-fp8" in ckpts[0]["inputs"]["ckpt_name"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_comfy_workflow.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.comfy_workflow'`

- [ ] **Step 3: Create `app/comfy_workflow.py`**

```python
"""Builds a ComfyUI prompt-graph for FLUX.1-schnell fp8.

The graph uses the single-file fp8 checkpoint (CheckpointLoaderSimple), which
bundles UNet + CLIP + VAE, so no separate loaders are needed.
"""

CKPT_NAME = "flux1-schnell-fp8.safetensors"


def build_flux_workflow(
    prompt: str,
    *,
    width: int = 768,
    height: int = 768,
    steps: int = 4,
    seed: int = 0,
) -> dict:
    return {
        "ckpt": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": CKPT_NAME},
        },
        "pos": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["ckpt", 1]},
        },
        "neg": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "", "clip": ["ckpt", 1]},
        },
        "latent": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "sampler": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
                "model": ["ckpt", 0],
                "positive": ["pos", 0],
                "negative": ["neg", 0],
                "latent_image": ["latent", 0],
            },
        },
        "decode": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["sampler", 0], "vae": ["ckpt", 2]},
        },
        "save": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "lineart", "images": ["decode", 0]},
        },
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_comfy_workflow.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/comfy_workflow.py tests/test_comfy_workflow.py
git commit -m "feat: add ComfyUI FLUX.1-schnell workflow builder"
```

---

### Task 5: ComfyUI client (submit → poll → fetch PNG)

**Files:**
- Create: `app/comfy_client.py`
- Create: `tests/test_comfy_client.py`

**Interfaces:**
- Consumes: `app.config.COMFYUI_BASE_URL`, `app.comfy_workflow.build_flux_workflow`.
- Produces: `async def generate_png(prompt: str, *, client: httpx.AsyncClient | None = None, poll_interval: float = 0.5, timeout_s: float = 120.0) -> bytes`.
  Flow: POST `{base}/prompt` with `{"prompt": graph, "client_id": "lineart"}` → read `prompt_id`;
  poll GET `{base}/history/{prompt_id}` until the entry exists and has `outputs.<node>.images[0]`;
  GET `{base}/view?filename=...&subfolder=...&type=...` → return PNG bytes.
  Raises `RuntimeError("ComfyUI unavailable at <url>: ...")` on connect errors,
  `RuntimeError("ComfyUI timed out ...")` if no image before `timeout_s`.
  Uses an injectable monotonic clock via param `now: callable = time.monotonic` and sleep via `sleep: callable = asyncio.sleep` for testability.

- [ ] **Step 1: Write the failing test `tests/test_comfy_client.py`**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_comfy_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.comfy_client'`

- [ ] **Step 3: Create `app/comfy_client.py`**

```python
import asyncio
import logging
import time

import httpx

from app import config
from app.comfy_workflow import build_flux_workflow

logger = logging.getLogger(__name__)

CLIENT_ID = "lineart"


async def generate_png(
    prompt: str,
    *,
    client: httpx.AsyncClient | None = None,
    poll_interval: float = 0.5,
    timeout_s: float = 120.0,
    now=time.monotonic,
    sleep=asyncio.sleep,
) -> bytes:
    """Run a FLUX workflow on the local ComfyUI server and return PNG bytes."""
    base = config.COMFYUI_BASE_URL
    graph = build_flux_workflow(prompt)

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=timeout_s)
    try:
        try:
            resp = await client.post(
                f"{base}/prompt", json={"prompt": graph, "client_id": CLIENT_ID}
            )
            resp.raise_for_status()
            prompt_id = resp.json()["prompt_id"]
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise RuntimeError(f"ComfyUI unavailable at {base}: {e}") from e

        deadline = now() + timeout_s
        image_info = None
        while now() < deadline:
            hist = await client.get(f"{base}/history/{prompt_id}")
            hist.raise_for_status()
            entry = hist.json().get(prompt_id)
            if entry:
                for node_out in entry.get("outputs", {}).values():
                    images = node_out.get("images")
                    if images:
                        image_info = images[0]
                        break
            if image_info:
                break
            await sleep(poll_interval)

        if image_info is None:
            raise RuntimeError(f"ComfyUI timed out after {timeout_s}s waiting for image")

        view = await client.get(
            f"{base}/view",
            params={
                "filename": image_info["filename"],
                "subfolder": image_info.get("subfolder", ""),
                "type": image_info.get("type", "output"),
            },
        )
        view.raise_for_status()
        return view.content
    finally:
        if owns_client:
            await client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_comfy_client.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/comfy_client.py tests/test_comfy_client.py
git commit -m "feat: add ComfyUI client (submit/poll/fetch PNG)"
```

---

### Task 6: Rewire `image_gen.py` to ComfyUI

**Files:**
- Modify: `app/image_gen.py` (replace HF call; keep `build_prompt` and `to_raw_mono`)
- Create: `tests/test_image_gen.py`

**Interfaces:**
- Consumes: `app.comfy_client.generate_png`, existing `build_prompt`, `to_raw_mono`.
- Produces: `async def generate_line_art(subject: str) -> tuple[str, str, str, int]` — same return tuple
  `(data_uri, prompt_used, raw_mono_b64, height)` as before, but NO `hf_token` parameter and it calls
  `comfy_client.generate_png(prompt)` for the image bytes. `build_prompt` and `to_raw_mono` are unchanged.

- [ ] **Step 1: Write the failing test `tests/test_image_gen.py`**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_image_gen.py -v`
Expected: FAIL (`image_gen` still imports/uses HF; `comfy_client` attr missing; signature has `hf_token`)

- [ ] **Step 3: Rewrite the generation parts of `app/image_gen.py`**

Replace the HF constants and `generate_with_huggingface`/`generate_line_art` with the below. KEEP `PROMPT_TEMPLATE`, `TARGET_WIDTH`, `build_prompt`, and `to_raw_mono` EXACTLY as they are.

```python
import base64
import io
import logging

from PIL import Image

from app import comfy_client

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = (
    "simple black and white line art drawing of {subject}, "
    "minimal style, clean lines, white background, no shading, outline only"
)

TARGET_WIDTH = 384


def build_prompt(subject: str) -> str:
    return PROMPT_TEMPLATE.format(subject=subject.strip())


# to_raw_mono(...) stays UNCHANGED — do not edit it.


async def generate_line_art(subject: str) -> tuple[str, str, str, int]:
    """Generate line art via local ComfyUI.

    Returns (base64_image_uri, prompt_used, base64_raw_mono, height).
    """
    prompt = build_prompt(subject)
    image_bytes = await comfy_client.generate_png(prompt)

    png_bytes, raw_bytes = to_raw_mono(image_bytes)
    image_b64 = base64.b64encode(png_bytes).decode()
    raw_b64 = base64.b64encode(raw_bytes).decode()
    height = len(raw_bytes) // 48  # 48 bytes per row

    return f"data:image/png;base64,{image_b64}", prompt, raw_b64, height
```

Note: `httpx` is no longer imported in this file. Ensure the `import httpx` line is removed and `to_raw_mono` (with its full existing body) is retained between `build_prompt` and `generate_line_art`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_image_gen.py tests/test_image_packing.py -v`
Expected: PASS (image_gen tests + packing guard still green)

- [ ] **Step 5: Commit**

```bash
git add app/image_gen.py tests/test_image_gen.py
git commit -m "feat: generate line art via local ComfyUI instead of HuggingFace"
```

---

### Task 7: Update `main.py` wiring and startup

**Files:**
- Modify: `app/main.py`
- Create: `tests/test_main_handlers.py`

**Interfaces:**
- Consumes: `app.image_gen.generate_line_art(subject)` (no token), `app.stt.transcribe`, `app.config`.
- Produces: unchanged WebSocket behavior; `handle_text_input` calls `generate_line_art(subject)` without a token; `lifespan` logs configured local URLs instead of API-key warnings; `HF_TOKEN`/`GROQ_API_KEY` references removed.

- [ ] **Step 1: Write the failing test `tests/test_main_handlers.py`**

```python
import pytest

from app import main


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


@pytest.mark.asyncio
async def test_handle_text_input_emits_progress_then_result(monkeypatch):
    async def fake_generate(subject):
        return ("data:image/png;base64,AAA", f"prompt for {subject}", "cm9hd19tb25v", 8)

    monkeypatch.setattr(main, "generate_line_art", fake_generate)
    ws = FakeWS()
    await main.handle_text_input(ws, "a cat")

    joined = " ".join(ws.sent)
    assert "progress" in joined
    assert "result" in joined
    assert "prompt for a cat" in joined


@pytest.mark.asyncio
async def test_handle_text_input_empty_sends_error():
    ws = FakeWS()
    await main.handle_text_input(ws, "   ")
    assert "error" in ws.sent[0]
    assert "Empty" in ws.sent[0]


@pytest.mark.asyncio
async def test_handle_text_input_reports_service_error(monkeypatch):
    async def boom(subject):
        raise RuntimeError("ComfyUI unavailable at http://localhost:8188")

    monkeypatch.setattr(main, "generate_line_art", boom)
    ws = FakeWS()
    await main.handle_text_input(ws, "a cat")
    assert "error" in ws.sent[-1]
    assert "ComfyUI unavailable" in ws.sent[-1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_main_handlers.py -v`
Expected: FAIL (`handle_text_input` still passes `HF_TOKEN`; import of removed name may error)

- [ ] **Step 3: Edit `app/main.py`**

Apply these specific changes:

1. Remove `HF_TOKEN = os.environ.get("HF_TOKEN")` and the `import os` use for it (keep `import os` only if still needed elsewhere; it is not — remove it).
2. Add `from app import config` near the other app imports.
3. Replace the `lifespan` body:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Server ready (offline). STT=Speaches@%s model=%s | ImageGen=ComfyUI@%s",
        config.SPEACHES_BASE_URL,
        config.SPEACHES_MODEL,
        config.COMFYUI_BASE_URL,
    )
    yield
```

4. In `handle_text_input`, change the generate call from
   `await generate_line_art(subject, HF_TOKEN)` to `await generate_line_art(subject)`.

Leave everything else (`send_json`, `handle_audio_input`, the `/ws` endpoint, `MAX_AUDIO_SIZE`, static mount) unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_main_handlers.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_main_handlers.py
git commit -m "feat: wire main to offline services, drop API-key startup checks"
```

---

### Task 8: Config files, requirements, and Speaches compose

**Files:**
- Modify: `.env.example`
- Modify: `.env` (local only; not committed — already gitignored)
- Modify: `requirements.txt`
- Create: `docker-compose.yml`

**Interfaces:**
- Consumes: nothing (operational files).
- Produces: runnable Speaches container on host port 8001; clean app requirements.

- [ ] **Step 1: Rewrite `.env.example`**

```
# Local Speaches (speech-to-text) server
SPEACHES_BASE_URL=http://localhost:8001
SPEACHES_MODEL=Systran/faster-whisper-large-v3

# Local ComfyUI (image generation) server
COMFYUI_BASE_URL=http://localhost:8188
```

- [ ] **Step 2: Update local `.env`**

Set the same three keys as `.env.example`. Remove `GROQ_API_KEY` and `HF_TOKEN` lines.

- [ ] **Step 3: Rewrite `requirements.txt`**

```
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
httpx>=0.27.0
Pillow>=10.0.0
pydantic>=2.0.0
python-dotenv>=1.0.0
```

- [ ] **Step 4: Create `docker-compose.yml` for Speaches**

```yaml
services:
  speaches:
    image: ghcr.io/speaches-ai/speaches:latest-cuda
    container_name: speaches
    restart: unless-stopped
    ports:
      - "8001:8000"   # host 8001 -> container 8000 (avoids FastAPI's 8000)
    volumes:
      - speaches-hub:/home/ubuntu/.cache/huggingface/hub
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

volumes:
  speaches-hub:
```

- [ ] **Step 5: Verify the compose file parses**

Run: `docker compose -f docker-compose.yml config`
Expected: prints the resolved config with no error. (Does NOT start the container.)

- [ ] **Step 6: Commit**

```bash
git add .env.example requirements.txt docker-compose.yml
git commit -m "chore: offline config, slim requirements, Speaches compose file"
```

---

### Task 9: README — offline setup docs

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing.
- Produces: documentation only.

- [ ] **Step 1: Replace the setup/architecture sections of `README.md`**

Add a "Fully Offline Setup" section covering, in order:

1. **Architecture diagram** (FastAPI 8000 → Speaches Docker 8001, ComfyUI native 8188).
2. **Speaches (STT):**
   - `docker compose up -d speaches`
   - Pull the model once:
     `curl -X POST "http://localhost:8001/v1/models/Systran/faster-whisper-large-v3"` (or via the Speaches UI at `http://localhost:8001`).
   - Verify: `curl http://localhost:8001/v1/models`.
3. **ComfyUI (image gen), native Windows:**
   - Install ComfyUI (portable build) per its README.
   - Download `flux1-schnell-fp8.safetensors` and place it in `ComfyUI/models/checkpoints/`.
   - Start: `python main.py --listen 0.0.0.0 --port 8188` (or the portable `run_nvidia_gpu.bat`).
   - Verify: open `http://localhost:8188`.
4. **App:**
   - `pip install -r requirements.txt`
   - `copy .env.example .env` (defaults already point at local services)
   - `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`
   - Open `http://127.0.0.1:8000/static/index.html`.
5. **Startup order:** Speaches + ComfyUI first, then the app. The app boots even if they are down; requests return a clear error until they are up.

Keep the existing WebSocket protocol / `raw_mono` format section unchanged.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: offline setup (Speaches + ComfyUI), update architecture"
```

---

### Task 10: Full verification pass

**Files:** none (verification only).

- [ ] **Step 1: Run the whole test suite**

Run: `python -m pytest -v`
Expected: ALL PASS (config, stt, image_packing, comfy_workflow, comfy_client, image_gen, main_handlers).

- [ ] **Step 2: Confirm no cloud references remain in app code**

Run: `git grep -nE "groq|huggingface|HF_TOKEN|GROQ_API_KEY|hf-inference" -- app/ requirements.txt .env.example`
Expected: NO matches.

- [ ] **Step 3: Import-smoke the app**

Run: `python -c "import app.main; print('import OK')"`
Expected: prints `import OK` with no error.

- [ ] **Step 4: (Manual, optional integration)**

With Speaches (8001) and ComfyUI (8188) running, start the app and use the browser client at `http://127.0.0.1:8000/static/index.html`: send a text subject, confirm a 1-bit image result; record audio, confirm transcription → image. Confirm no outbound internet traffic is required.

- [ ] **Step 5: Final commit (if any docs/cleanup touched)**

```bash
git add -A
git commit -m "chore: final offline verification pass" || echo "nothing to commit"
```

---

## Self-Review Notes

- **Spec coverage:** stt→Speaches (T2), image_gen→ComfyUI (T4/T5/T6), main wiring + startup (T7), config/ports/requirements/compose (T1/T8), README (T9), unchanged bitmap format guarded (T3), no-cloud-fallback error path (T2/T5/T7), boot-without-services (T7 lifespan). All spec sections mapped.
- **Placeholders:** none — every code/test step has full content.
- **Type consistency:** `transcribe(audio_bytes, client=None)`, `build_flux_workflow(prompt, *, ...)`, `generate_png(prompt, *, client=None, ...)`, `generate_line_art(subject)` used consistently across tasks and tests. `SaveImage` node key `"save"` matches the client's output-scan (which iterates all output nodes, so key name is not load-bearing).
```
