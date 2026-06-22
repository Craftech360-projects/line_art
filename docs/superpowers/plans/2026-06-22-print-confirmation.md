# Print Confirmation Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate FLUX image generation on a user decision — after `line_art_transcription`, the device session waits for `print_confirm` (generate) or `print_reject` (abort) instead of generating automatically.

**Architecture:** Split the current `_run_line_art` (which transcribes AND generates) into `_transcribe_and_prompt` (decode → transcribe → send `line_art_transcription`, returns the text) and `_generate_and_send` (progress → generate → `line_art`). The receive loop in `handle_device_session` stores a `pending_text` after transcription and dispatches `print_confirm`/`print_reject` to generate or clear it. Device protocol only; browser path untouched.

**Tech Stack:** Python 3.11, Starlette WebSocket, pytest + pytest-asyncio.

## Global Constraints

- Change is **device protocol only** (`app/device_protocol.py`). Do NOT touch `handle_text_input` / `handle_audio_input` / the browser path / `app/models.py`.
- Outbound message names stay `line_art_*` (`line_art_transcription`, `line_art_progress`, `line_art`, `line_art_error`) — the firmware expects them. Do NOT rename to `transcription`/`result`.
- New inbound (device→server) text frames: `{"type":"print_confirm"}` and `{"type":"print_reject"}`, no payload beyond `type`.
- After `print_confirm`, ALWAYS terminate with exactly one `line_art` OR `line_art_error` (device waits in DRAWING with no client timeout).
- `print_reject` → clear pending, send NOTHING.
- `print_confirm` with no pending transcription → ignore (no-op), send nothing.
- A new `listen start` (new audio) clears any pending un-confirmed transcription.
- Empty/failed STT → send `line_art_error` immediately, leave `pending_text = None` (no confirm needed for an error).
- Keep the injectable kwargs `transcribe` / `generate_line_art` / `decode` on `handle_device_session` for testing.
- Keep existing behavior: hello reply first, `SAVE_DEVICE_AUDIO` debug dump, disconnect-flush of buffered AUDIO (never auto-generate an un-confirmed pending_text).
- Tests use the existing `FakeWS` / `_text` / `_bytes` helpers in `tests/test_device_protocol.py`; mock `transcribe`/`generate_line_art`/`decode`; never hit real services.

---

### Task 1: Gate generation behind print_confirm/print_reject

**Files:**
- Modify: `app/device_protocol.py` (refactor `_run_line_art`; add `pending_text` + dispatch)
- Modify: `tests/test_device_protocol.py` (add confirm-flow tests; fix the two existing tests that assumed auto-generation)

**Interfaces:**
- Consumes: `app.device_messages` builders (`line_art_transcription`, `line_art_progress`, `line_art`, `line_art_error`), `app.stt.transcribe`, `app.image_gen.generate_line_art`, `app.opus_decode.decode_opus_to_wav`.
- Produces:
  - `async def _transcribe_and_prompt(ws, session_id, opus_frames, transcribe, decode) -> str | None` — decode → optional debug-WAV → transcribe; on empty/error send `line_art_error` and return `None`; else send `line_art_transcription` and return the stripped text.
  - `async def _generate_and_send(ws, session_id, text, generate_line_art) -> None` — send `line_art_progress(stage="image_gen")` → generate → send `line_art` (or `line_art_error` on failure).
  - `handle_device_session(...)` keeps its signature; internally tracks `pending_text` and dispatches `print_confirm`/`print_reject`.

- [ ] **Step 1: Update the existing tests to the new gated behavior + add new tests**

In `tests/test_device_protocol.py`, the two existing tests that drive a full listen-cycle and expect a `line_art` (`test_full_listen_cycle_emits_line_art_sequence`) or a flush-generation (`test_disconnect_mid_listen_does_not_generate` already expects NO generation — keep it) must reflect that generation now waits for `print_confirm`. Replace `test_full_listen_cycle_emits_line_art_sequence` with a version that confirms, and ADD the new cases.

Add a `_confirm` / `_reject` helper near `_text`/`_bytes` and these tests (keep the existing `FakeWS`, `_text`, `_bytes`, `test_hello_reply_sent_first`, `test_empty_transcript_emits_error_not_line_art`, `test_generate_failure_emits_error`, `test_disconnect_mid_listen_does_not_generate`):

```python
def _confirm():
    return _text({"type": "print_confirm"})


def _reject():
    return _text({"type": "print_reject"})


@pytest.mark.asyncio
async def test_transcription_waits_for_confirm_then_generates():
    captured = {}

    async def fake_transcribe(wav):
        return "a cat"

    async def fake_generate(subject):
        captured["subject"] = subject
        return ("data:image/png;base64,AAA", f"prompt {subject}", "cmF3bW9ubw==", 240)

    def fake_decode(frames, sample_rate=16000):
        return b"RIFF"

    events = [
        _text({"type": "listen", "state": "start"}),
        _bytes(b"op"),
        _text({"type": "listen", "state": "stop"}),
        _confirm(),
    ]
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"},
        transcribe=fake_transcribe, generate_line_art=fake_generate, decode=fake_decode,
    )
    types = [m["type"] for m in ws.sent]
    # transcription is sent and comes before any generation output
    assert "line_art_transcription" in types
    assert "line_art" in types
    assert types.index("line_art_transcription") < types.index("line_art_progress") < types.index("line_art")
    assert captured["subject"] == "a cat"
    final = next(m for m in ws.sent if m["type"] == "line_art")
    assert final["raw_mono"] == "cmF3bW9ubw==" and final["width"] == 384 and final["height"] == 240


@pytest.mark.asyncio
async def test_transcription_alone_does_not_generate():
    # listen-stop produces a transcription but NO confirm arrives -> no generation.
    async def fake_transcribe(wav):
        return "a cat"

    async def fake_generate(subject):
        raise AssertionError("generate must not run before print_confirm")

    events = [
        _text({"type": "listen", "state": "start"}),
        _bytes(b"op"),
        _text({"type": "listen", "state": "stop"}),
        # no confirm -> session ends
    ]
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"},
        transcribe=fake_transcribe, generate_line_art=fake_generate,
        decode=lambda f, sample_rate=16000: b"RIFF",
    )
    types = [m["type"] for m in ws.sent]
    assert "line_art_transcription" in types
    assert "line_art" not in types
    assert "line_art_progress" not in types


@pytest.mark.asyncio
async def test_reject_sends_nothing_and_does_not_generate():
    async def fake_transcribe(wav):
        return "a cat"

    async def fake_generate(subject):
        raise AssertionError("generate must not run on print_reject")

    events = [
        _text({"type": "listen", "state": "start"}),
        _bytes(b"op"),
        _text({"type": "listen", "state": "stop"}),
        _reject(),
    ]
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"},
        transcribe=fake_transcribe, generate_line_art=fake_generate,
        decode=lambda f, sample_rate=16000: b"RIFF",
    )
    types = [m["type"] for m in ws.sent]
    assert "line_art_transcription" in types
    assert "line_art" not in types
    assert "line_art_progress" not in types
    assert "line_art_error" not in types


@pytest.mark.asyncio
async def test_new_audio_voids_pending_then_confirm_uses_new_text():
    texts = iter(["old fox", "new owl"])

    async def fake_transcribe(wav):
        return next(texts)

    seen = {}

    async def fake_generate(subject):
        seen["subject"] = subject
        return ("data:image/png;base64,AAA", "p", "cmF3", 240)

    events = [
        _text({"type": "listen", "state": "start"}),   # first utterance
        _bytes(b"op"),
        _text({"type": "listen", "state": "stop"}),     # -> transcribe "old fox"
        _text({"type": "listen", "state": "start"}),     # NEW audio voids "old fox"
        _bytes(b"op2"),
        _text({"type": "listen", "state": "stop"}),      # -> transcribe "new owl"
        _confirm(),                                       # confirm -> generate "new owl"
    ]
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"},
        transcribe=fake_transcribe, generate_line_art=fake_generate,
        decode=lambda f, sample_rate=16000: b"RIFF",
    )
    assert seen["subject"] == "new owl"


@pytest.mark.asyncio
async def test_confirm_with_no_pending_is_ignored():
    async def fake_generate(subject):
        raise AssertionError("generate must not run with no pending transcription")

    events = [_confirm()]   # confirm with nothing pending
    ws = FakeWS(events)
    await device_protocol.handle_device_session(
        ws, {"type": "hello"},
        transcribe=lambda w: "x", generate_line_art=fake_generate,
        decode=lambda f, sample_rate=16000: b"RIFF",
    )
    # only the hello reply was sent
    assert [m["type"] for m in ws.sent] == ["hello"]
```

Also REPLACE the body of the existing `test_full_listen_cycle_emits_line_art_sequence` (it currently expects `line_art` right after `listen stop`) — either delete it (its coverage is now in `test_transcription_waits_for_confirm_then_generates`) or rename it. Simplest: delete `test_full_listen_cycle_emits_line_art_sequence` entirely.

Keep `test_generate_failure_emits_error` but it must now send a `print_confirm` to reach generation. Update its events to:

```python
    events = [
        _text({"type": "listen", "state": "start"}),
        _bytes(b"x"),
        _text({"type": "listen", "state": "stop"}),
        _text({"type": "print_confirm"}),
    ]
```
(its assertion that a `line_art_error` with `stage == "image_gen"` is sent stays).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_device_protocol.py -v`
Expected: FAIL — the new tests fail because generation isn't gated yet (e.g. `test_transcription_alone_does_not_generate` sees a `line_art`, `test_confirm_with_no_pending_is_ignored` etc.).

- [ ] **Step 3: Refactor `app/device_protocol.py` — split the pipeline and gate it**

Replace the single `_run_line_art` function with the two functions below, and rewrite the `listen`/dispatch section of `handle_device_session` to track `pending_text` and handle `print_confirm`/`print_reject`.

First, replace `_run_line_art` (lines ~102-132) with:

```python
async def _transcribe_and_prompt(ws, session_id, opus_frames, transcribe, decode):
    """Decode + transcribe; send line_art_transcription. Returns the text to
    print (the pending prompt), or None if STT was empty/failed (error sent)."""
    try:
        wav = decode(opus_frames)
        if SAVE_DEVICE_AUDIO:
            _save_debug_wav(session_id, wav)
        text = (await transcribe(wav)).strip()
    except Exception as e:
        logger.exception("STT failed")
        await ws.send_json(dm.line_art_error(f"Transcription failed: {e}", stage="stt", session_id=session_id))
        return None

    if not text:
        await ws.send_json(dm.line_art_error(
            "Could not transcribe any speech from audio.", stage="stt", session_id=session_id))
        return None

    await ws.send_json(dm.line_art_transcription(text, session_id=session_id))
    return text


async def _generate_and_send(ws, session_id, text, generate_line_art):
    """Generate the bitmap for a confirmed prompt and send line_art (or error)."""
    await ws.send_json(dm.line_art_progress(
        f"Generating line art for '{text}'...", stage="image_gen", session_id=session_id))
    try:
        _data_uri, _prompt, raw_mono, height = await generate_line_art(text)
    except Exception as e:
        logger.exception("Image generation failed")
        await ws.send_json(dm.line_art_error(str(e), stage="image_gen", session_id=session_id))
        return
    await ws.send_json(dm.line_art(raw_mono, 384, height, session_id=session_id))
```

Then in `handle_device_session`, add `pending_text = None` next to the other state vars (after `opus_frames: list[bytes] = []`):

```python
    pending_text = None  # transcription awaiting print_confirm / print_reject
```

And replace the text-frame dispatch block (the `if data.get("type") == "listen":` section, lines ~69-82) with:

```python
                mtype_in = data.get("type")
                if mtype_in == "listen":
                    state = data.get("state")
                    if state == "start":
                        listening = True
                        opus_frames = []
                        pending_text = None  # new audio voids any un-confirmed prompt
                    elif state == "stop":
                        if not listening:
                            continue
                        listening = False
                        pending_text = await _transcribe_and_prompt(
                            ws, session_id, opus_frames, transcribe, decode,
                        )
                        opus_frames = []
                elif mtype_in == "print_confirm":
                    if pending_text:
                        text = pending_text
                        pending_text = None
                        await _generate_and_send(ws, session_id, text, generate_line_art)
                    # no pending -> ignore
                elif mtype_in == "print_reject":
                    pending_text = None  # abort; send nothing
                # other text types (mcp, hello repeats, etc.) are ignored
```

Leave the binary-frame handling, the disconnect handling, and the `finally` flush as they are — BUT the flush block currently calls the now-deleted `_run_line_art`. Update the `finally` flush to NOT auto-generate (per spec, an un-confirmed prompt is never auto-generated). Replace the `finally` body:

```python
    finally:
        # No auto-generation on session end: generation only happens on an
        # explicit print_confirm. Buffered audio that never got a listen-stop is
        # simply dropped. (pending_text, if any, was never confirmed -> void.)
        pass
    logger.info("Device session %s ended", session_id)
```

Note: this removes the old "best-effort flush" that ran `_run_line_art` on disconnect. Since generation is now gated on `print_confirm`, flushing un-confirmed audio would generate something the user never approved — which the spec forbids. The `disconnected` variable may become unused; leave it (it still documents the disconnect break) or remove it if your linter complains — removing it is fine as long as the `mtype == "websocket.disconnect": break` line stays.

- [ ] **Step 4: Run the device-protocol tests to verify they pass**

Run: `python -m pytest tests/test_device_protocol.py -v`
Expected: PASS — all confirm-flow tests plus the kept ones (hello, empty-transcript error, generate-failure-after-confirm, disconnect-no-generate).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `python -m pytest -q`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add app/device_protocol.py tests/test_device_protocol.py
git commit -m "feat: gate device image-gen behind print_confirm/print_reject"
```

---

### Task 2: Manual end-to-end verification

**Files:** none (verification only). Requires the app on :8090 + Speaches + ComfyUI running.

- [ ] **Step 1: Confirm the full suite is green**

Run: `python -m pytest -q`
Expected: ALL PASS.

- [ ] **Step 2: Drive the gated flow with a quick script**

Create a throwaway check (do not commit) that sends hello → listen start → opus frames → listen stop, asserts a `line_art_transcription` arrives and NO `line_art` yet, then sends `print_confirm` and asserts `line_art` arrives. Reuse `ai_printer_client`'s encode helper:

```python
import asyncio, json, base64, websockets
from app.opus_decode import _encode_pcm_to_opus
import numpy as np

async def main():
    pcm = (np.sin(2*np.pi*330*np.linspace(0,1.0,16000,endpoint=False))*9000).astype(np.int16)
    frames = _encode_pcm_to_opus(pcm)
    async with websockets.connect("ws://localhost:8090/ws", max_size=None) as ws:
        await ws.send(json.dumps({"type":"hello","version":1,"transport":"websocket",
            "audio_params":{"format":"opus","sample_rate":16000,"channels":1,"frame_duration":60}}))
        print("hello reply:", json.loads(await ws.recv())["type"])
        await ws.send(json.dumps({"type":"listen","state":"start"}))
        for f in frames: await ws.send(f)
        await ws.send(json.dumps({"type":"listen","state":"stop"}))
        # expect transcription, and NO line_art yet
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        print("after stop:", m["type"])   # line_art_transcription
        assert m["type"] == "line_art_transcription"
        # now confirm
        await ws.send(json.dumps({"type":"print_confirm"}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=600))
            print("  ", m["type"])
            if m["type"] in ("line_art","line_art_error"):
                if m["type"]=="line_art":
                    raw=base64.b64decode(m["raw_mono"]); assert len(raw)==m["height"]*48
                    print("PASS: confirmed -> printed", m["width"],"x",m["height"])
                break

asyncio.run(main())
```

Run it. Expected: prints `line_art_transcription` after stop (NOT `line_art`), then after `print_confirm` prints progress then `line_art` (or `line_art_error` if ComfyUI is cold/unavailable) and `PASS`.

- [ ] **Step 3: Verify reject sends nothing**

Repeat the script but send `{"type":"print_reject"}` instead of confirm, then assert the socket receives no further message within a short timeout (e.g. wrap `ws.recv()` in `asyncio.wait_for(..., timeout=3)` and expect `asyncio.TimeoutError`). Expected: timeout (server sent nothing). Clean up the throwaway script (do not commit).

---

## Self-Review Notes

- **Spec coverage:** gate-until-confirm (T1 dispatch + `_transcribe_and_prompt`/`_generate_and_send` split), `print_confirm`→generate (T1), `print_reject`→silent abort (T1), confirm-with-no-pending ignored (T1), new-audio voids pending (T1), empty/failed STT→error without confirm (T1 `_transcribe_and_prompt`), always terminate after confirm with line_art/error (T1 `_generate_and_send`), no auto-generate on disconnect (T1 `finally`), device-only/line_art_* names kept (constraints), manual e2e (T2). All spec sections mapped.
- **Placeholder scan:** none — all code/test steps are complete.
- **Type consistency:** `_transcribe_and_prompt(ws, session_id, opus_frames, transcribe, decode) -> str|None` and `_generate_and_send(ws, session_id, text, generate_line_art)` are used identically in the dispatch. `generate_line_art` returns the existing 4-tuple `(data_uri, prompt_used, raw_mono, height)`; `_generate_and_send` uses index 2 (`raw_mono`) + 3 (`height`), width fixed 384 — matches the prior `_run_line_art`. `pending_text` is the single new state var, set by `_transcribe_and_prompt`'s return and consumed by `print_confirm`.
