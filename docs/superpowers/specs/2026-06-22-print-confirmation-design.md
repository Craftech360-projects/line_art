# Design: Print Confirmation Flow (device protocol)

**Date:** 2026-06-22
**Status:** Approved
**Source:** device-team change spec; wire contract in [`aiprinter-server-contract.md`](../../../aiprinter-server-contract.md)

## Goal

Gate FLUX image generation on an explicit user confirmation. Today the device
session transcribes **and** generates in one shot. Image generation is the
slow/expensive step, so the device now asks the user "print this?" right after
the transcription, and the server must **wait** for the user's decision before
generating.

## Scope

- **Device protocol only** (`app/device_protocol.py`, the Opus/`line_art_*`
  path). The browser/text path (`handle_text_input` / `handle_audio_input`) is
  unchanged — it has no confirm UI and keeps generating immediately.
- Message names stay `line_art_*` (the firmware expects them per the contract).
  Only the **timing/gating** changes, plus two new inbound decision messages.

## New inbound messages (device → server, text frames)

| Message | Meaning | Server must do |
|---|---|---|
| `{"type":"print_confirm"}` | user accepted the transcription | generate the bitmap, send exactly one `line_art` (or `line_art_error`) |
| `{"type":"print_reject"}` | user rejected | abort; send nothing further for this prompt |

They carry no payload beyond `type`; the server correlates them with the most
recent transcription on that connection (one prompt in flight at a time).

## Behavior

**Before:** `listen stop` → decode → transcribe → `line_art_transcription` →
`line_art_progress` → generate → `line_art`.

**After:** `listen stop` → decode → transcribe → `line_art_transcription` →
**PAUSE (store pending_text)** → wait for the device's decision:
- `print_confirm` → `line_art_progress` → generate → `line_art` (or `line_art_error`) → clear pending.
- `print_reject` → clear pending, send nothing.

## Architecture

A single new piece of per-session state in the existing receive loop of
`handle_device_session`: `pending_text: str | None` (the transcription awaiting
a decision; `None` when idle). No blocking await — the loop keeps dispatching,
which naturally handles new audio, disconnect, and other frames while waiting.

### Receive-loop dispatch (additions in **bold**)

| Incoming frame | Action |
|---|---|
| `listen` `start` | reset Opus buffer, mark listening, **clear `pending_text`** (a new audio upload voids any prior un-confirmed prompt) |
| `listen` `stop` | decode → (save debug WAV) → transcribe → send `line_art_transcription` → **set `pending_text`** (do NOT generate). Empty/failed STT → `line_art_error`, leave `pending_text = None`. |
| **`print_confirm`** | if `pending_text` is set: `line_art_progress` → generate → `line_art`/`line_art_error`; then clear `pending_text`. If not set: ignore (no-op). |
| **`print_reject`** | clear `pending_text`; send nothing. |
| binary frame | append to Opus buffer while listening (unchanged) |
| other text types | ignored (unchanged) |

### Splitting `_run_line_art`

The current `_run_line_art` (decode+transcribe **and** generate) splits into two:

- `async _transcribe_and_prompt(ws, session_id, opus_frames, transcribe, decode) -> str | None`
  decode → optional debug-WAV save → transcribe → on empty/error send
  `line_art_error` and return `None`; otherwise send `line_art_transcription`
  and return the text (the new `pending_text`).
- `async _generate_and_send(ws, session_id, text, generate_line_art)`
  send `line_art_progress(stage="image_gen")` → generate → send `line_art`
  (or `line_art_error` on failure).

The handler keeps its injectable `transcribe` / `generate_line_art` / `decode`
kwargs for testing.

## Data flow

```
device → server : listen start
device → server : <opus frames>
device → server : listen stop
server → device : line_art_transcription { text }
                  ── server stores pending_text; NO generation ──
device          : "print this? RECORD=print / CANCEL=reject"

  ┌ confirm ───────────────────────────────────────────┐
  device → server : { "type": "print_confirm" }
  server → device : line_art_progress { stage:"image_gen" }
  server → device : line_art { raw_mono, width, height }   (or line_art_error)
  └────────────────────────────────────────────────────┘

  ┌ reject ────────────────────────────────────────────┐
  device → server : { "type": "print_reject" }
  server          : clears pending_text; sends nothing
  └────────────────────────────────────────────────────┘
```

## Error handling (per spec obligations)

- After `print_confirm`, **always** terminate with `line_art` or
  `line_art_error` — the device sits in "DRAWING…" with no client-side timeout
  and must be released.
- `print_reject` → silent abort (no ack, nothing sent).
- `print_confirm` with no pending transcription → ignored (no-op), nothing sent.
- Empty/failed STT → `line_art_error` immediately (no confirm needed for an
  error); `pending_text` stays `None`.
- Disconnect while awaiting confirm → session ends; the device returns to idle
  on reconnect. The existing best-effort flush flushes buffered **audio** only
  (when listening and not disconnected) — it never auto-generates an
  un-confirmed pending_text.

## Non-goals (YAGNI)

- No change to the browser/text path.
- No `print_confirm`/`print_reject` outbound builders (they are inbound only;
  parsed by `type`).
- No server-side confirm timeout (none on the client; a new audio upload or
  disconnect voids a stuck pending prompt).
- No new dependencies.

## Testing strategy

Unit tests on `handle_device_session` with a `FakeWS` and injected fakes:

1. **Confirm path:** listen-start → bytes → listen-stop → assert
   `line_art_transcription` sent and **generate NOT called yet**; then
   `print_confirm` → assert `line_art` sent (generate called exactly once).
2. **Reject path:** … → `line_art_transcription` → `print_reject` → assert no
   `line_art` / `line_art_progress` sent and generate never called.
3. **Void-on-new-audio:** transcription pending → new listen-start/stop →
   confirming generates the **new** text, not the old.
4. **Confirm with no pending:** `print_confirm` alone → nothing sent, generate
   not called.
5. **Generate fails after confirm:** → `line_art_error` sent.
6. Existing tests (hello reply, empty-transcript error, disconnect flush) stay
   green.

Also update `aiprinter-server-contract.md` with a "Print confirmation" section
documenting `print_confirm` / `print_reject` and the gated sequence.
