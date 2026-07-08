# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

# Project: line_art (AI Printer + AI Imagine)

Python/FastAPI service that turns **voice or text into images** for the Cheeko toy.
Two features share one pipeline:

- **AI Printer** (original): speak a prompt → 1-bit line-art bitmap → thermal printer.
- **AI Imagine** (newer): speak a prompt → color JPEG → shown on the toy's 320×240 LCD.

```
voice/text → STT (Whisper) → moderation → FLUX image gen → bitmap/JPEG → back over /ws
```

Run: `uvicorn app.main:app --host 0.0.0.0 --port 8090 --reload` (port 8090, NOT 8000 — taken on dev machine).
Tests: `python -m pytest -q`

## Single /ws endpoint, two protocols

`app/main.py` sniffs the **first message** on `/ws`:

- JSON `hello` → **device protocol** → `app/device_protocol.py`. Optionally auth-gated by `WS_SHARED_SECRET`.
- Anything else → **browser protocol** (test pages in `static/`): `{"type":"text_input","text":"..."}` JSON or raw WAV bytes.

## Device protocol flow

The **mqtt-gateway** (cheeko-backend) connects on the device's behalf:

1. `hello` → server replies with session_id (must reply < 10 s)
2. `listen {state:start}` → server buffers raw Opus frames (16 kHz mono, 60 ms, **no Ogg container**)
3. `listen {state:stop}` → decode → transcribe → `line_art_transcription`

Mode diverges on `feature` in the hello:

- **Printer mode** (default): transcription becomes a *pending prompt*; generation waits for `print_confirm` (`print_reject` aborts). Result: `line_art {raw_mono, width:384, height}` — packed 1-bit bitmap, MSB-first, 48 bytes/row, brightness threshold (`MONO_THRESHOLD=190`, NOT dithering — keeps lines solid).
- **Imagine mode** (`feature: "ai_imagine"`): no confirm gate — generates immediately, sends base64 color JPEG in an `image` message. The gateway uploads to S3/CDN and forwards a URL to the device (ADR-0001). line_art never speaks MQTT.

Guards: utterances < `MIN_UTTERANCE_FRAMES` (5 ≈ 300 ms) skipped (Whisper hallucinates on silence, each hallucination costs a full image gen); audio > 10 MB rejected; empty STT → `line_art_error {stage:"stt"}`.

## Critical gotchas

- **Opus decode uses `opuslib` (direct libopus), NOT PyAV** — PyAV's FFmpeg wrapper forces 48 kHz output and corrupts the 16 kHz stream.
- **README's "fully offline" claim is stale** — `app/config.py` defaults are CLOUD (Groq STT, HuggingFace FLUX, Groq moderation); local (Speaches :8001, ComfyUI :8188) is opt-in via `STT_BACKEND`/`IMAGE_BACKEND`.
- `COMFYUI_TIMEOUT_S=20` must stay BELOW the gateway's `IMAGINE_TIMEOUT_MS` (90 s) so line_art always resolves first.
- Image *generation* failures serve `fallback.jpg` so the toy still gets a picture; **safety blocks are never replaced by the fallback**.
- `aiprinter-server-contract.md` is the authoritative device wire contract. Glossary: `CONTEXT.md`. ADRs: `docs/adr/`.

## STT provider selection (ADR-0002)

Active provider fetched from cheeko-backend manager-api (`GET /providers/active`, cached `STT_PROVIDER_TTL_S`=300 s) — admin-selected, **shared with the picoclaw voice agent**. On hard failure (5xx, timeout, 429, auth, connection) falls back to `STT_LAST_RESORT_PROVIDER` (Groq). A 200 with empty/garbage text = "no speech", NOT a hard failure. Providers: groq, deepgram, sarvam (`app/stt_providers.py`).

## System context

Cheeko device speaks MQTT (control) + UDP (Opus audio) to **mqtt-gateway**:

- **AI Chat**: gateway → LiveKit room → **picoclaw** Go agent (`D:\picoclaw`, STT→LLM→TTS). Not this repo.
- **AI Imagine/Printer**: gateway takes the "gateway shortcut" — pipes raw Opus straight into this service's `/ws` device protocol, bypassing LiveKit/picoclaw.

## Key files

| File | Role |
|------|------|
| `app/main.py` | FastAPI app, /ws protocol router, browser handlers |
| `app/device_protocol.py` | device session: hello, Opus buffering, print_confirm gate, imagine mode |
| `app/device_messages.py` | builders for server→device JSON |
| `app/config.py` | all env config (backends, keys, timeouts) |
| `app/stt.py` / `app/stt_providers.py` | STT dispatch + provider adapters |
| `app/manager_client.py` | manager-api active-provider fetch + cache |
| `app/opus_decode.py` | raw Opus → WAV via opuslib |
| `app/image_gen.py` | FLUX call, 384px resize, 1-bit threshold, fallback image |
| `app/comfy_client.py` / `comfy_workflow.py` | ComfyUI submit/poll/fetch + FLUX prompt graph |
| `app/moderation.py` | pluggable moderation providers (groq/openai/openrouter/openai_moderation), active one from manager-api, Groq env last resort, fails open |
| `ai_printer_client.py` / `ai_printer_gui.py` | CLI / Tkinter test clients (real Opus encoding) |

Debug knobs: `SAVE_DEVICE_AUDIO=1` (decoded WAVs → `debug_audio/`), `SAVE_INPUT_AUDIO=1` (browser WAVs), `SAVE_GENERATED_IMAGES=1` (PNGs → `generated_images/`; default OFF in prod — children's data).