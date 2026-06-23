# Browser-Path Print Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate FLUX generation on the browser/text WebSocket path — after audio is transcribed, the server sends `transcription` and waits for `print_confirm` (generate) or `print_reject` (abort) instead of generating immediately.

**Architecture:** Add a per-connection `pending_text: str | None` to the `/ws` browser receive loop in `app/main.py`. Refactor `handle_audio_input` to stop after sending `transcription` and return the text (no generation). Thread `pending_text` through `_process_browser_message`, which dispatches binary→transcribe, `print_confirm`→generate the pending text, `print_reject`→clear, and typed `text`→generate immediately. Browser path only; device `line_art_*` path untouched.

**Tech Stack:** Python 3.11, FastAPI/Starlette WebSocket, pytest + pytest-asyncio.

## Global Constraints

- Change is **browser/text path only** (`app/main.py`). Do NOT touch `app/device_protocol.py`, `app/models.py`, `app/image_gen.py`, `app/stt.py`.
- Browser outbound message names stay as-is: `progress`, `transcription`, `result`, `error` (from `ProgressMessage`/`TranscriptionMessage`/`ResultMessage`/`ErrorMessage` in `app/models.py`). Do NOT rename to `line_art_*`.
- New inbound (device→server) text frames: `{"type":"print_confirm"}` and `{"type":"print_reject"}`, no payload beyond `type`.
- **Audio is gated; typed text is NOT.** A binary WAV frame → transcribe → `transcription` → pause. A typed `{"type":"text_input","text":...}` frame → generate immediately (unchanged).
- After `print_confirm`, ALWAYS terminate with exactly one `result` OR `error` (firmware waits in STATE_WAIT_BITMAP with no client timeout).
- `print_reject` → clear pending, send NOTHING.
- `print_confirm` with no pending transcription → ignore (no-op), send nothing.
- A new audio (binary) frame clears any pending un-confirmed transcription; a typed-text frame also clears it.
- Empty/failed STT → send `error(stage="stt")` immediately, leave `pending_text = None`.
- The `/ws` first-message peek (hello→`handle_device_session`) is unchanged.
- Tests use a local `FakeWS` (scripted `receive()`, captures `send_text`/`send_json`); mock `transcribe`/`generate_line_art` via `monkeypatch.setattr(main, ...)`; never hit real services.

---

### Task 1: Gate the browser/text path behind print_confirm/print_reject

**Files:**
- Modify: `app/main.py` (refactor `handle_audio_input`; thread `pending_text` through `_process_browser_message` and the `/ws` loop)
- Modify: `tests/test_ws_dispatch.py` (add browser-gating tests)

**Interfaces:**
- Consumes: `app.main.transcribe`, `app.main.generate_line_art`, models `ProgressMessage(stage, message)`, `TranscriptionMessage(text)`, `ResultMessage(image, prompt_used, raw_mono, height)`, `ErrorMessage(stage, message)`, `TextInput(type, text)`, helper `send_json(ws, msg)`.
- Produces:
  - `async def handle_audio_input(ws, audio_bytes) -> str | None` — sends `progress(stt)`; on STT failure/empty sends `error(stage="stt")` and returns `None`; on success sends `transcription(text)` and returns the text. Does NOT generate.
  - `async def _process_browser_message(ws, message, pending_text) -> str | None` — dispatches one frame and returns the new `pending_text`.
  - `handle_text_input(ws, subject)` unchanged (still `progress(generating)` → `result`/`error`).

- [ ] **Step 1: Add the browser-gating tests**

In `tests/test_ws_dispatch.py`, the existing `FakeWS` only stores `sent` and has `receive`/`send_text`/`send_json`. Add a `_bytes` helper next to `_text`, and add these tests. Keep the existing `test_hello_routes_to_device_handler` and `test_text_input_still_uses_browser_handler`.

Add at the top helper area (after `_text`):

```python
def _bytes(b):
    return {"type": "websocket.receive", "bytes": b}


def _sent_types(ws):
    """Decode each captured send into its 'type' field (sends are JSON strings)."""
    out = []
    for s in ws.sent:
        out.append(json.loads(s)["type"])
    return out
```

Then add the tests:

```python
@pytest.mark.asyncio
async def test_audio_waits_for_confirm_then_generates(monkeypatch):
    captured = {}

    async def fake_transcribe(audio):
        return "a cat"

    async def fake_generate(subject):
        captured["subject"] = subject
        return ("data:image/png;base64,AAA", f"prompt {subject}", "cmF3", 240)

    monkeypatch.setattr(main, "transcribe", fake_transcribe)
    monkeypatch.setattr(main, "generate_line_art", fake_generate)

    ws = FakeWS([_bytes(b"WAVDATA"), _text({"type": "print_confirm"})])
    await main.websocket_endpoint(ws)

    types = _sent_types(ws)
    assert "transcription" in types
    assert "result" in types
    # transcription is sent before any generating-progress or result
    assert types.index("transcription") < types.index("result")
    assert captured["subject"] == "a cat"


@pytest.mark.asyncio
async def test_audio_alone_does_not_generate(monkeypatch):
    async def fake_transcribe(audio):
        return "a cat"

    async def fake_generate(subject):
        raise AssertionError("generate must not run before print_confirm")

    monkeypatch.setattr(main, "transcribe", fake_transcribe)
    monkeypatch.setattr(main, "generate_line_art", fake_generate)

    ws = FakeWS([_bytes(b"WAVDATA")])  # no confirm
    await main.websocket_endpoint(ws)

    types = _sent_types(ws)
    assert "transcription" in types
    assert "result" not in types


@pytest.mark.asyncio
async def test_reject_sends_nothing_and_does_not_generate(monkeypatch):
    async def fake_transcribe(audio):
        return "a cat"

    async def fake_generate(subject):
        raise AssertionError("generate must not run on print_reject")

    monkeypatch.setattr(main, "transcribe", fake_transcribe)
    monkeypatch.setattr(main, "generate_line_art", fake_generate)

    ws = FakeWS([_bytes(b"WAVDATA"), _text({"type": "print_reject"})])
    await main.websocket_endpoint(ws)

    types = _sent_types(ws)
    assert "transcription" in types
    assert "result" not in types
    assert "error" not in types


@pytest.mark.asyncio
async def test_confirm_with_no_pending_is_ignored(monkeypatch):
    async def fake_generate(subject):
        raise AssertionError("generate must not run with no pending transcription")

    monkeypatch.setattr(main, "generate_line_art", fake_generate)

    ws = FakeWS([_text({"type": "print_confirm"})])
    await main.websocket_endpoint(ws)

    assert ws.sent == []  # nothing sent at all


@pytest.mark.asyncio
async def test_new_audio_voids_pending_then_confirm_uses_new_text(monkeypatch):
    texts = iter(["old fox", "new owl"])

    async def fake_transcribe(audio):
        return next(texts)

    seen = {}

    async def fake_generate(subject):
        seen["subject"] = subject
        return ("data:image/png;base64,AAA", "p", "cmF3", 240)

    monkeypatch.setattr(main, "transcribe", fake_transcribe)
    monkeypatch.setattr(main, "generate_line_art", fake_generate)

    ws = FakeWS([_bytes(b"one"), _bytes(b"two"), _text({"type": "print_confirm"})])
    await main.websocket_endpoint(ws)

    assert seen["subject"] == "new owl"


@pytest.mark.asyncio
async def test_typed_text_still_generates_immediately(monkeypatch):
    seen = {}

    async def fake_generate(subject):
        seen["subject"] = subject
        return ("data:image/png;base64,AAA", "p", "cmF3", 240)

    async def boom_transcribe(audio):
        raise AssertionError("transcribe must not run for typed text")

    monkeypatch.setattr(main, "generate_line_art", fake_generate)
    monkeypatch.setattr(main, "transcribe", boom_transcribe)

    ws = FakeWS([_text({"type": "text_input", "text": "a dog"})])
    await main.websocket_endpoint(ws)

    types = _sent_types(ws)
    assert "result" in types
    assert seen["subject"] == "a dog"


@pytest.mark.asyncio
async def test_empty_stt_sends_error_and_confirm_is_noop(monkeypatch):
    async def fake_transcribe(audio):
        return ""

    async def fake_generate(subject):
        raise AssertionError("generate must not run after empty STT")

    monkeypatch.setattr(main, "transcribe", fake_transcribe)
    monkeypatch.setattr(main, "generate_line_art", fake_generate)

    ws = FakeWS([_bytes(b"WAVDATA"), _text({"type": "print_confirm"})])
    await main.websocket_endpoint(ws)

    types = _sent_types(ws)
    assert "error" in types
    assert "result" not in types
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_ws_dispatch.py -v`
Expected: FAIL — e.g. `test_audio_alone_does_not_generate` sees a `result` (audio still auto-generates), `test_confirm_with_no_pending_is_ignored` may error on the unhandled `print_confirm` frame (parsed as a `TextInput` → `error` sent, so `ws.sent != []`).

- [ ] **Step 3: Refactor `handle_audio_input` to stop after transcription**

In `app/main.py`, replace the body of `handle_audio_input` (currently ends by calling `handle_text_input`) so it returns the text instead of generating. Replace lines ~81-105 (`async def handle_audio_input ... await handle_text_input(ws, text)`) with:

```python
async def handle_audio_input(ws: WebSocket, audio_bytes: bytes) -> str | None:
    """Process audio -> transcription. Sends `transcription` and RETURNS the text
    (the pending prompt) — generation is gated behind a later print_confirm.
    Returns None if the audio was too large or STT was empty/failed (error sent)."""
    MAX_AUDIO_SIZE = 10 * 1024 * 1024  # ~10MB
    if len(audio_bytes) > MAX_AUDIO_SIZE:
        await send_json(ws, ErrorMessage(stage="input", message="Audio too large. Keep recordings under 10 seconds."))
        return None

    logger.info("Audio received: %d bytes (%.1f KB)", len(audio_bytes), len(audio_bytes) / 1024)
    await send_json(ws, ProgressMessage(stage="stt", message="Transcribing audio..."))

    try:
        text = await transcribe(audio_bytes)
    except Exception as e:
        logger.exception("Transcription failed")
        await send_json(ws, ErrorMessage(stage="stt", message=f"Transcription failed: {e}"))
        return None

    if not text:
        logger.warning("STT returned empty transcription")
        await send_json(ws, ErrorMessage(stage="stt", message="Could not transcribe any speech from audio."))
        return None

    logger.info("Transcription result: '%s'", text)
    await send_json(ws, TranscriptionMessage(text=text))
    return text
```

- [ ] **Step 4: Thread `pending_text` through `_process_browser_message` and the loop**

Replace `_process_browser_message` (lines ~108-118) with a version that takes and returns `pending_text`:

```python
async def _process_browser_message(ws: WebSocket, message: dict, pending_text):
    """Handle one browser-protocol frame. `pending_text` is the transcription
    awaiting a decision (or None). Returns the new pending_text."""
    if "bytes" in message and message["bytes"] is not None:
        # New audio voids any prior un-confirmed transcription.
        return await handle_audio_input(ws, message["bytes"])

    if "text" in message and message["text"] is not None:
        try:
            data = json.loads(message["text"])
        except json.JSONDecodeError as e:
            await send_json(ws, ErrorMessage(stage="input", message=f"Invalid message: {e}"))
            return pending_text

        mtype = data.get("type") if isinstance(data, dict) else None
        if mtype == "print_confirm":
            if pending_text:
                await handle_text_input(ws, pending_text)
            return None  # consumed (or no-op if nothing pending)
        if mtype == "print_reject":
            return None  # abort; send nothing

        # Otherwise treat it as a typed text_input (generates immediately).
        try:
            parsed = TextInput(**data)
        except (TypeError, ValueError) as e:
            await send_json(ws, ErrorMessage(stage="input", message=f"Invalid message: {e}"))
            return pending_text
        await handle_text_input(ws, parsed.text)
        return None  # typed text also clears any pending audio prompt

    return pending_text
```

Then update the `/ws` loop (lines ~141-148) to carry `pending_text`:

```python
        # Not a device hello: process this first message, then continue the
        # existing browser loop.
        pending_text = await _process_browser_message(ws, first, None)
        while True:
            message = await ws.receive()
            if message.get("type") != "websocket.receive":
                if message.get("type") == "websocket.disconnect":
                    break
                continue
            pending_text = await _process_browser_message(ws, message, pending_text)
```

Note: `TextInput(**data)` previously raised `ValueError`; pydantic raises `ValidationError` (a subclass of `ValueError`) plus may raise `TypeError` for non-dict — both are caught above. The `json.JSONDecodeError` is caught separately so a malformed text frame still yields the existing `Invalid message` error.

- [ ] **Step 5: Run the dispatch tests to verify they pass**

Run: `python -m pytest tests/test_ws_dispatch.py -v`
Expected: PASS — all new browser-gating tests plus the two kept ones (`test_hello_routes_to_device_handler`, `test_text_input_still_uses_browser_handler`).

- [ ] **Step 6: Run the full suite (no regressions)**

Run: `python -m pytest -q`
Expected: ALL PASS. In particular `tests/test_main_handlers.py` (direct `handle_text_input` tests) is unaffected because `handle_text_input` is unchanged.

- [ ] **Step 7: Commit**

```bash
git add app/main.py tests/test_ws_dispatch.py
git commit -m "feat: gate browser/text path image-gen behind print_confirm/print_reject"
```

---

### Task 2: Update the wire contract doc

**Files:**
- Modify: `aiprinter-server-contract.md` (document that the browser path is now gated too)

**Interfaces:**
- Consumes: nothing (documentation only).
- Produces: nothing code references.

- [ ] **Step 1: Add a browser-path confirmation note to the contract**

In `aiprinter-server-contract.md`, find the existing "Cheeko-specific: print confirmation (device → server)" section (around line 122). Immediately AFTER that section's "Notes" list (after the line ``- A `print_confirm` with no pending transcription is ignored.``), add this subsection verbatim:

```markdown
### Browser/text path: print confirmation (the AiPrinter `transcription`/`result` firmware)

The AiPrinter device (firmware `AiPrinterCFT`) speaks the **browser protocol**
message names — `progress`, `transcription`, `result`, `error` — and sends a
full WAV blob as one binary frame (no `hello`, no Opus). The server routes it to
the browser path, which is **also gated** on the same `print_confirm` /
`print_reject` frames:

```
device → server : (binary) WAV
server → device : progress { stage: "stt" }
server → device : transcription { text }
                  ── server PAUSES; no generation yet ──
  print_confirm → progress { stage:"generating" } → result { raw_mono, width, height }  (or error)
  print_reject  → server aborts, sends nothing
```

Gating applies to **audio** only — a typed `text_input` frame still generates
immediately. A `print_confirm` with no pending transcription is ignored; a new
audio frame voids any prior un-confirmed transcription. After `print_confirm`
the server always terminates with exactly one `result` or `error` (the firmware
waits in its DRAWING state with no client-side timeout).
```

- [ ] **Step 2: Commit**

```bash
git add aiprinter-server-contract.md
git commit -m "docs: document browser-path print confirmation in server contract"
```

---

### Task 3: Manual end-to-end verification

**Files:** none (verification only). Requires the app on :8090 + Speaches + ComfyUI running, restarted WITHOUT `--reload` (reload drops live WebSocket sessions on file writes).

- [ ] **Step 1: Confirm the full suite is green**

Run: `python -m pytest -q`
Expected: ALL PASS.

- [ ] **Step 2: Drive the gated browser flow with a throwaway script**

Create a throwaway check under `.superpowers/sdd/` (git-ignored; do not commit) that connects, sends a WAV blob, asserts `transcription` arrives and NO `result` yet, then sends `print_confirm` and asserts `result` arrives. Build a tiny valid WAV in-memory:

```python
import asyncio, json, base64, struct, math
import websockets

def make_wav(seconds=1.0, rate=16000, freq=330):
    n = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))
    data_len = len(pcm)
    header = b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", data_len)
    return header + pcm

async def main():
    async with websockets.connect("ws://localhost:8090/ws", max_size=None) as ws:
        await ws.send(make_wav())
        # expect progress(stt) then transcription, and NO result yet
        got_transcription = False
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            print("  ", m["type"], m.get("text", ""))
            if m["type"] == "transcription":
                got_transcription = True
                break
            assert m["type"] != "result", "result arrived before confirm!"
        assert got_transcription
        # ensure server PAUSES (no further message for a moment)
        try:
            extra = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            raise SystemExit(f"FAIL: server sent {extra['type']} before confirm")
        except asyncio.TimeoutError:
            print("OK: paused after transcription")
        # confirm
        await ws.send(json.dumps({"type": "print_confirm"}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=600))
            print("  ", m["type"])
            if m["type"] in ("result", "error"):
                if m["type"] == "result":
                    raw = base64.b64decode(m["raw_mono"]); assert len(raw) == m["height"] * 48
                    print("PASS: confirmed -> result", m["width"], "x", m["height"])
                break

asyncio.run(main())
```

Run it (with the repo root importable / from the repo dir). Expected: prints `progress`/`transcription`, "OK: paused", then after confirm prints `progress` then `result` (or `error` if ComfyUI is down) and `PASS`.

- [ ] **Step 3: Verify reject sends nothing**

Repeat the script but send `{"type":"print_reject"}` instead of `print_confirm`, then assert the socket receives nothing within ~4 s (wrap `ws.recv()` in `asyncio.wait_for(..., timeout=4)` and expect `asyncio.TimeoutError`). Expected: timeout (server sent nothing). Clean up the throwaway script (do not commit).

- [ ] **Step 4 (optional): Verify with the real device**

Restart the server WITHOUT `--reload`, then run the AiPrinter device: record → speak → it should show "HEARD: <text> / RECORD=PRINT THIS / CANCEL=REJECT" (STATE_CONFIRM), and the server log should show `Transcription result:` then PAUSE (no immediate `Image generated`). Press RECORD → device sends `print_confirm` → server generates → device prints (no more "Bitmap arrived in state 3; discarding").

---

## Self-Review Notes

- **Spec coverage:** audio gated until confirm (T1 `_process_browser_message` binary→`handle_audio_input` returns pending), `print_confirm`→generate (T1), `print_reject`→silent (T1), confirm-with-no-pending ignored (T1), new-audio voids pending (T1, binary always overwrites pending), typed text immediate (T1), empty/failed STT→error without confirm (T1 `handle_audio_input`), always terminate after confirm with result/error (T1 reuses `handle_text_input`), hello-routing unchanged (T1 loop untouched above the browser branch), contract doc (T2), manual e2e (T3). All spec sections mapped.
- **Placeholder scan:** none — all code/test steps are complete.
- **Type consistency:** `handle_audio_input(ws, audio_bytes) -> str | None` returns the pending text; `_process_browser_message(ws, message, pending_text) -> str | None` consumes/returns it; the `/ws` loop stores it in `pending_text`. `handle_text_input(ws, subject)` is reused unchanged and returns None (its return is ignored). `TextInput` requires `type="text_input"` + `text`; `print_confirm`/`print_reject` are dispatched BEFORE the `TextInput(**data)` parse so they never raise. `ResultMessage` 4-tuple `(image, prompt_used, raw_mono, height)` is produced inside `handle_text_input` (unchanged). Models `ProgressMessage(stage, message)`, `TranscriptionMessage(text)`, `ErrorMessage(stage, message)` match `app/models.py`.
