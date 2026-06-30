# AI Imagine — line_art Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an "imagine mode" to the line_art server that turns a spoken prompt into a color JPEG (≤320×240, ≤200 KB) and returns the bytes over the existing device WebSocket, leaving the thermal-printer path untouched.

**Architecture:** The mqtt-gateway impersonates the device and opens line_art's existing WS session with `feature:"ai_imagine"` in the `hello`. line_art branches: it reuses Opus decode + Whisper STT unchanged, but instead of waiting for `print_confirm` and emitting a 1-bit `line_art` message, it generates a color image immediately and emits a new `image` message carrying base64 JPEG bytes. Upload/CDN/MQTT all happen in the gateway (out of scope here). See [CONTEXT.md](../../CONTEXT.md) and [ADR-0001](../adr/0001-imagine-image-delivery-via-gateway-upload.md).

**Tech Stack:** Python 3.11, FastAPI/Starlette WebSocket, Pillow, httpx, HuggingFace FLUX.1-schnell, Groq Whisper (dev), pytest.

## Global Constraints

- Device image: **baseline (non-progressive) JPEG**, **exactly 320×240**, **24-bit RGB**, **≤ 200 KB** (`200 * 1024` bytes).
- Imagine session is identified by `feature == "ai_imagine"` in the device `hello` (session-level / spec Option A).
- Imagine mode **never** waits for `print_confirm` — generate immediately after transcription.
- The **printer path must not change** — no edits to `generate_line_art`, `to_raw_mono`, `build_prompt`, or the `print_confirm`/`line_art` flow.
- line_art does **not** touch S3/MQTT. Errors are reported with the existing `line_art_error` builder; the gateway maps them to device `image_error` codes.
- Image generation reuses `generate_with_huggingface` (the live FLUX path), only the prompt + post-processing differ.

---

### Task 1: Color-JPEG conversion + imagine generator (`image_gen.py`)

**Files:**
- Modify: `D:\line_art\app\image_gen.py` (add constants + 3 functions; do not touch existing ones)
- Test: `D:\line_art\tests\test_imagine_gen.py`

**Interfaces:**
- Consumes: `generate_with_huggingface(prompt: str) -> bytes` (existing, returns PNG bytes).
- Produces:
  - `build_imagine_prompt(subject: str) -> str`
  - `to_device_jpeg(image_bytes: bytes) -> bytes` — center-cropped 4:3, 320×240, baseline JPEG ≤200 KB, RGB.
  - `generate_imagine_jpeg(subject: str) -> tuple[bytes, str]` — returns `(jpeg_bytes, prompt_used)`.

- [ ] **Step 1: Write the failing test**

```python
# D:\line_art\tests\test_imagine_gen.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_imagine_gen.py -v`
Expected: FAIL — `AttributeError: module 'app.image_gen' has no attribute 'to_device_jpeg'`

- [ ] **Step 3: Write minimal implementation**

Append to `D:\line_art\app\image_gen.py` (do not modify existing functions):

```python
# --- AI Imagine: color JPEG path (separate from the 1-bit printer path) ---

DEVICE_W, DEVICE_H = 320, 240
MAX_JPEG_BYTES = 200 * 1024

IMAGINE_PROMPT_TEMPLATE = (
    "a colorful, friendly children's illustration of {subject}, "
    "bright cartoon style, simple shapes, clean plain background, cheerful, safe for kids"
)


def build_imagine_prompt(subject: str) -> str:
    return IMAGINE_PROMPT_TEMPLATE.format(subject=subject.strip())


def to_device_jpeg(image_bytes: bytes) -> bytes:
    """Center-crop to 4:3, resize to 320x240, return baseline JPEG <= 200 KB (RGB)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    target = DEVICE_W / DEVICE_H  # 4:3
    if w / h > target:  # too wide -> crop sides
        new_w = int(round(h * target))
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    elif w / h < target:  # too tall -> crop top/bottom
        new_h = int(round(w / target))
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    img = img.resize((DEVICE_W, DEVICE_H), Image.LANCZOS)

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
    prompt = build_imagine_prompt(subject)
    image_bytes = await generate_with_huggingface(prompt)
    return to_device_jpeg(image_bytes), prompt
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_imagine_gen.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/image_gen.py tests/test_imagine_gen.py
git commit -m "feat(imagine): color 320x240 JPEG generator"
```

---

### Task 2: `image` message builder (`device_messages.py`)

**Files:**
- Modify: `D:\line_art\app\device_messages.py` (add one builder)
- Test: `D:\line_art\tests\test_imagine_messages.py`

**Interfaces:**
- Consumes: `_with_session(msg, session_id)` (existing).
- Produces: `image(image_b64: str, width: int, height: int, caption: str | None = None, mime: str = "image/jpeg", session_id: str | None = None) -> dict`

- [ ] **Step 1: Write the failing test**

```python
# D:\line_art\tests\test_imagine_messages.py
from app import device_messages as dm


def test_image_message_shape():
    msg = dm.image("QUJD", 320, 240, caption="a cat", session_id="s1")
    assert msg["type"] == "image"
    assert msg["session_id"] == "s1"
    assert msg["image"] == "QUJD"
    assert msg["mime"] == "image/jpeg"
    assert msg["width"] == 320 and msg["height"] == 240
    assert msg["caption"] == "a cat"


def test_image_message_omits_caption_when_none():
    msg = dm.image("QUJD", 320, 240)
    assert "caption" not in msg
    assert "session_id" not in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_imagine_messages.py -v`
Expected: FAIL — `AttributeError: module 'app.device_messages' has no attribute 'image'`

- [ ] **Step 3: Write minimal implementation**

Append to `D:\line_art\app\device_messages.py`:

```python
def image(image_b64: str, width: int, height: int, caption: str | None = None,
          mime: str = "image/jpeg", session_id: str | None = None) -> dict:
    """AI Imagine result sent to the gateway: base64 JPEG bytes + dimensions.

    The gateway uploads the bytes to S3 and builds the device-facing `image{url}`.
    """
    msg = {"type": "image", "image": image_b64, "mime": mime,
           "width": width, "height": height}
    if caption is not None:
        msg["caption"] = caption
    return _with_session(msg, session_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_imagine_messages.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/device_messages.py tests/test_imagine_messages.py
git commit -m "feat(imagine): image message builder (base64 jpeg)"
```

---

### Task 3: Imagine-mode session branch (`device_protocol.py`)

**Files:**
- Modify: `D:\line_art\app\device_protocol.py` (add `base64` import, one helper, branch in `handle_device_session`)
- Test: `D:\line_art\tests\test_imagine_protocol.py`

**Interfaces:**
- Consumes: `image_gen.generate_imagine_jpeg(subject) -> (bytes, str)`, `dm.image(...)`, existing `_transcribe_and_prompt`.
- Produces: imagine branch — when `first_message["feature"] == "ai_imagine"`, a `listen/stop` triggers immediate generation and an `image` message; no `print_confirm` is expected. New kwarg `generate_imagine=image_gen.generate_imagine_jpeg` for test injection.

- [ ] **Step 1: Write the failing test**

```python
# D:\line_art\tests\test_imagine_protocol.py
import base64
import pytest
from app import device_protocol


class FakeWS:
    """Minimal Starlette-WebSocket double driven by a scripted inbound queue."""
    def __init__(self, inbound):
        self._inbound = list(inbound)
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)

    async def receive(self):
        if self._inbound:
            return self._inbound.pop(0)
        return {"type": "websocket.disconnect"}


@pytest.mark.asyncio
async def test_imagine_session_emits_image_without_print_confirm():
    inbound = [
        {"type": "websocket.receive", "text": '{"type":"listen","state":"start"}'},
        {"type": "websocket.receive", "bytes": b"\x01\x02"},
        {"type": "websocket.receive", "text": '{"type":"listen","state":"stop"}'},
        {"type": "websocket.disconnect"},
    ]
    ws = FakeWS(inbound)

    async def fake_transcribe(wav):  # bypass STT
        return "a blue dog"

    def fake_decode(frames):
        return b"WAVDATA"

    async def fake_generate(subject):
        return b"JPEGBYTES", f"prompt::{subject}"

    await device_protocol.handle_device_session(
        ws,
        {"type": "hello", "feature": "ai_imagine"},
        transcribe=fake_transcribe,
        decode=fake_decode,
        generate_imagine=fake_generate,
    )

    types = [m["type"] for m in ws.sent]
    assert "line_art_transcription" in types
    assert "image" in types  # generated WITHOUT any print_confirm
    img = next(m for m in ws.sent if m["type"] == "image")
    assert base64.b64decode(img["image"]) == b"JPEGBYTES"
    assert img["caption"] == "a blue dog"
    assert "line_art" not in types  # printer message must NOT be emitted


@pytest.mark.asyncio
async def test_chat_printer_path_unchanged_still_requires_confirm():
    # No feature flag -> classic path: transcription, then NOTHING until print_confirm.
    inbound = [
        {"type": "websocket.receive", "text": '{"type":"listen","state":"start"}'},
        {"type": "websocket.receive", "bytes": b"\x01"},
        {"type": "websocket.receive", "text": '{"type":"listen","state":"stop"}'},
        {"type": "websocket.disconnect"},
    ]
    ws = FakeWS(inbound)

    async def fake_transcribe(wav):
        return "a cat"

    def fake_decode(frames):
        return b"WAV"

    async def fake_imagine(subject):
        raise AssertionError("imagine generator must not run on the printer path")

    await device_protocol.handle_device_session(
        ws, {"type": "hello"},
        transcribe=fake_transcribe, decode=fake_decode, generate_imagine=fake_imagine,
    )
    types = [m["type"] for m in ws.sent]
    assert "line_art_transcription" in types
    assert "image" not in types
    assert "line_art" not in types  # never confirmed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_imagine_protocol.py -v`
Expected: FAIL — `TypeError: handle_device_session() got an unexpected keyword argument 'generate_imagine'`

- [ ] **Step 3: Write minimal implementation**

Edit `D:\line_art\app\device_protocol.py`:

3a. Add `import base64` near the top imports (after `import json`).

3b. Add this helper after `_generate_and_send`:

```python
async def _generate_imagine_and_send(ws, session_id, text, generate_imagine):
    """Imagine mode: generate a color JPEG immediately (no print_confirm) and
    send it as an `image` message. The gateway uploads it and builds image{url}."""
    await ws.send_json(dm.line_art_progress(
        f"Imagining '{text}'...", stage="image_gen", session_id=session_id))
    try:
        jpeg, _prompt = await generate_imagine(text)
    except Exception as e:
        logger.exception("Imagine generation failed")
        await ws.send_json(dm.line_art_error(str(e), stage="image_gen", session_id=session_id))
        return
    image_b64 = base64.b64encode(jpeg).decode()
    await ws.send_json(dm.image(image_b64, 320, 240, caption=text, session_id=session_id))
```

3c. Change the `handle_device_session` signature to add the kwarg:

```python
async def handle_device_session(
    ws,
    first_message: dict,
    *,
    transcribe=stt.transcribe,
    generate_line_art=image_gen.generate_line_art,
    generate_imagine=image_gen.generate_imagine_jpeg,
    decode=opus_decode.decode_opus_to_wav,
) -> None:
```

3d. Right after `session_id = uuid.uuid4().hex`, add:

```python
    imagine = first_message.get("feature") == "ai_imagine"
```

3e. Replace the existing `elif state == "stop":` block body with:

```python
                    elif state == "stop":
                        if not listening:
                            continue
                        listening = False
                        text = await _transcribe_and_prompt(
                            ws, session_id, opus_frames, transcribe, decode,
                        )
                        opus_frames = []
                        if imagine:
                            if text:
                                await _generate_imagine_and_send(
                                    ws, session_id, text, generate_imagine)
                            pending_text = None  # imagine never waits for confirm
                        else:
                            pending_text = text
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_imagine_protocol.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the full suite to confirm the printer path still works**

Run: `python -m pytest -q`
Expected: PASS (all existing tests + the 3 new files green)

- [ ] **Step 6: Commit**

```bash
git add app/device_protocol.py tests/test_imagine_protocol.py
git commit -m "feat(imagine): session branch emits image, skips print_confirm"
```

---

## Self-Review

- **Spec coverage:** Opus reuse (untouched decode) ✓; STT reuse ✓; feature flag in hello (Option A) ✓; skip print_confirm ✓; color JPEG ≤320×240 ≤200KB baseline RGB ✓; bytes returned to gateway (not URL) per ADR-0001 ✓; printer path unchanged (asserted by `test_chat_printer_path_unchanged`) ✓. Out of scope for this plan (gateway/manager-api): S3 upload, `image{url}`/`image_status`/`image_error` device messages, MQTT, per-session serialization.
- **Placeholder scan:** none — all steps contain runnable code/commands.
- **Type consistency:** `generate_imagine_jpeg -> (bytes, str)` consumed identically in Task 3; `dm.image(...)` signature matches Task 2; `to_device_jpeg -> bytes` used in Task 1's `generate_imagine_jpeg`.
