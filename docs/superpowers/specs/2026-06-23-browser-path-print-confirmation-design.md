# Design: Print Confirmation Flow — Browser/Text Path

**Date:** 2026-06-23
**Status:** Approved
**Source:** AiPrinter firmware (`AiPrinterCFT/main/main.c`); wire models in `app/models.py`.
**Related:** [`2026-06-22-print-confirmation-design.md`](2026-06-22-print-confirmation-design.md) gated the **device** (`line_art_*`) path. This spec gates the **browser/text** path the same way.

## Goal

Gate FLUX image generation on the browser/text WebSocket path behind an explicit
`print_confirm`. After transcription, the server sends `transcription` and
**waits** for the user's decision instead of generating immediately.

## Why (root cause)

The real AI-printer device runs the `AiPrinterCFT` firmware, which:
- speaks the **browser protocol** message names — `progress`, `transcription`,
  `result`, `error` (NOT the `line_art_*` names),
- sends a complete **WAV blob** as one binary frame (no `hello`, no Opus), so the
  server's `/ws` peek routes it to the **browser path**
  (`handle_audio_input`/`handle_text_input`), not `handle_device_session`,
- runs a state machine `IDLE → RECORDING → SENDING(3) → CONFIRM(4) →
  WAIT_BITMAP(5) → PRINTING`, and accepts a `result` bitmap **only** in
  `WAIT_BITMAP` (after it has sent `{"type":"print_confirm"}`).

Because the browser path is **not** gated, it auto-generates and sends `result`
while the device is still in `STATE_SENDING(3)` waiting for `transcription`. The
firmware logs `Bitmap arrived in state 3; discarding` and the print is lost.

## Scope

- **Browser/text path only** — [`app/main.py`](../../../app/main.py): the `/ws`
  loop, `handle_audio_input`, and the dispatch of typed input.
- **Unchanged:** `app/device_protocol.py` (device `line_art_*` path),
  `app/models.py`, `app/image_gen.py`, `app/stt.py`.
- Message names stay the browser names (`progress`, `transcription`, `result`,
  `error`) — the firmware expects them. Only **timing/gating** changes, plus the
  two inbound decision messages.

## Decision: audio gated, typed text immediate

- **Audio** (WAV binary frame) → transcribe → `transcription` → **pause** for
  `print_confirm`/`print_reject`. This is the real device flow.
- **Typed text** (`{"text": "..."}`) → generate immediately (unchanged). Typing
  IS the confirmation; only the test web UI/GUI uses typed input — the firmware
  never does.

## Inbound messages (device → server, text frames)

Reuse the same frames as the device path:

| Message | Meaning | Server must do |
|---|---|---|
| `{"type":"print_confirm"}` | user accepted the transcription | generate, send `progress(generating)` + exactly one `result` (or `error`) |
| `{"type":"print_reject"}` | user rejected | clear pending; send nothing further for this prompt |

No payload beyond `type`. Correlated with the most recent `transcription` on the
connection (one prompt in flight at a time).

## Architecture (Option A — per-connection `pending_text`)

A single new per-connection state variable in the existing `/ws` receive loop:
`pending_text: str | None` (the transcription awaiting a decision; `None` when
idle). No blocking await — the loop keeps dispatching, which naturally handles
new audio, typed input, disconnect, and the decision frames.

### Receive-loop dispatch

| Incoming frame | Action |
|---|---|
| binary (WAV) | `handle_audio_input` → on success send `progress(stt)` + `transcription`, **set `pending_text`**; do NOT generate. Empty/failed STT → `error`, leave `pending_text = None`. A new audio frame voids any prior un-confirmed `pending_text`. |
| `{"type":"print_confirm"}` | if `pending_text`: clear it, then `handle_text_input(pending_text)` (→ `progress(generating)` → `result`/`error`). If not set: ignore (no-op). |
| `{"type":"print_reject"}` | clear `pending_text`; send nothing. |
| `{"text": "..."}` (typed `TextInput`) | `handle_text_input(text)` immediately (unchanged); also clears `pending_text`. |
| other / invalid text | `error(stage="input", "Invalid message: ...")` for malformed `TextInput` (unchanged); unknown `type` ignored. |

### Function changes

- **`handle_audio_input(ws, audio_bytes) -> str | None`** — refactor: drop the
  tail call to `handle_text_input`. Sends `progress(stt)` then, on success,
  `transcription(text)` and **returns `text`**. On transcribe failure → send
  `error(stage="stt")`, return `None`. On empty transcript → send
  `error(stage="stt")`, return `None`.
- **`handle_text_input(ws, subject) -> None`** — unchanged. Still sends
  `progress(generating)` → `result`/`error`. Called directly for typed input and
  by the loop for a confirmed audio prompt.
- **`_process_browser_message`** — its logic moves into the `/ws` loop (or the
  loop is given the `pending_text` it must read/mutate). The loop distinguishes:
  binary → audio; text `{"type":"print_confirm"/"print_reject"}` → decision;
  other text → typed `TextInput`.

The first-message peek in `/ws` is unchanged: a `hello` first frame still routes
to `handle_device_session`; anything else enters the browser loop, whose first
message is processed by the same dispatch as subsequent ones.

## Data flow

```
device → server : (binary) WAV
server → device : progress { stage: "stt" }            ("Transcribing audio...")
server → device : transcription { text }
                  ── server stores pending_text; NO generation ──
device          : STATE_CONFIRM — "HEARD: <text> / RECORD=PRINT THIS / CANCEL=REJECT"

  ┌ confirm ───────────────────────────────────────────┐
  device → server : { "type": "print_confirm" }          (device → STATE_WAIT_BITMAP)
  server → device : progress { stage: "generating" }     ("DRAWING...")
  server → device : result { raw_mono, width, height }    (or error) → device prints
  └────────────────────────────────────────────────────┘

  ┌ reject ────────────────────────────────────────────┐
  device → server : { "type": "print_reject" }
  server          : clears pending_text; sends nothing    (device already idle)
  └────────────────────────────────────────────────────┘
```

## Error handling (per firmware obligations)

- After `print_confirm`, **always** terminate with `result` or `error` — the
  firmware sits in `STATE_WAIT_BITMAP` with no client-side timeout and must be
  released.
- `print_reject` → silent abort (nothing sent; the device is already idle).
- `print_confirm` with no pending transcription → ignored (no-op), nothing sent.
- Empty/failed STT → `error(stage="stt")` immediately (no confirm needed for an
  error); `pending_text` stays `None`.
- Generation failure after confirm → `error(stage="image_gen")` (existing
  `handle_text_input` behavior) → firmware leaves DRAWING.
- Disconnect while awaiting confirm → loop ends; nothing is generated. An
  un-confirmed prompt is never auto-generated.

## Non-goals (YAGNI)

- No change to the device `line_art_*` path.
- No gating of typed text input.
- No `print_confirm`/`print_reject` outbound builders (inbound only; parsed by
  `type`).
- No server-side confirm timeout (none on the client; new audio or disconnect
  voids a stuck pending prompt).
- No new dependencies; no new message models (reuse `app/models.py`).

## Testing strategy

A `FakeWS` (scripted `receive()`, captures sent JSON) drives the `/ws` browser
loop with mocked `transcribe` / `generate_line_art` (never hit real services):

1. **Confirm path:** binary WAV → assert `transcription` sent and **generate NOT
   called yet**; then `print_confirm` → assert `result` sent (generate called
   once), in order `transcription` → `progress(generating)` → `result`.
2. **Audio alone does not generate:** WAV, no confirm → `transcription` present,
   no `result`/`progress(generating)`.
3. **Reject path:** WAV → `transcription` → `print_reject` → no `result`, no
   generating-progress, no `error`.
4. **Confirm with no pending:** `print_confirm` alone → nothing sent, generate
   not called.
5. **New audio voids pending:** WAV("old") → WAV("new") → `print_confirm` →
   generate called with "new".
6. **Typed text still immediate:** `{"text":"a cat"}` → `result` sent with no
   confirm, generate called once.
7. **Empty/failed STT → error, no pending:** transcribe returns "" → `error`
   sent; a following `print_confirm` is a no-op (nothing generated).

Also update `aiprinter-server-contract.md` to document that the browser path is
now gated (the `transcription`/`result` names) and that both paths accept
`print_confirm` / `print_reject`.
