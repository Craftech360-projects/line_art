# Design: Cheeko Device WebSocket Protocol

**Date:** 2026-06-19
**Status:** Approved
**Source of truth:** [aiprinter-server-contract.md](../../../aiprinter-server-contract.md) (derived from the Cheeko firmware)

## Goal

Make the existing `/ws` endpoint speak the **AI Printer Cheeko** firmware's
WebSocket protocol so the device connects, streams voice, and prints the FLUX
line-art bitmap — fully offline. The device currently fails because our server
does not answer its `hello` handshake.

## Core insight

Our pipeline already produces exactly what the device prints: `raw_mono`
(384px wide, 48 bytes/row, 1-bit MSB-first). This is a **protocol translation
layer**, not new image logic. Our internal flow maps 1:1 onto the device's
Cheeko-specific message types:

| Internal pipeline step | Device message sent |
|---|---|
| transcription | `line_art_transcription {text}` |
| progress | `line_art_progress {message, stage}` |
| result | `line_art {raw_mono, width, height}` |
| error | `line_art_error {message, stage}` |

## Protocol facts (from the contract)

- Device connects to `ws://<host>:8090/ws`, sends a `hello` first, and requires
  a `hello` reply (with `"transport":"websocket"`) **within 10 s** or it aborts.
- Audio is **raw Opus** binary frames (protocol v1, no header), 16 kHz mono,
  60 ms. Sent after `{"type":"listen","state":"start"}`, ended by
  `{"type":"listen","state":"stop"}`.
- Server→device messages are text frames with a `type`. The device echoes
  `session_id` if the server provided one in the hello.
- Cheeko-specific print messages: `line_art_transcription`, `line_art_progress`,
  `line_art_error`, `line_art`. Send progress first, then exactly one `line_art`
  or `line_art_error` to release the device's line-art watchdog.

## Architecture

`/ws` auto-detects the client by its first message:
- first message `type == "hello"` → **device protocol** (new)
- first message is `text_input` (or binary) → **existing browser protocol** (unchanged)

```
Device ──ws──> /ws ── first msg type? ──hello──► device_protocol.handle_device_session
                                       └─other─► existing browser handlers (unchanged)

device session:
  1. hello ──────────► reply hello {type, transport:websocket, session_id, audio_params}
  2. listen start ───► begin buffering binary Opus frames
  3. <opus frames> ──► accumulate in a list
  4. listen stop ────► decode Opus→WAV (PyAV) ─► stt.transcribe (Speaches)
                       ─► line_art_transcription {text}
                       ─► line_art_progress {generating}
                       ─► image_gen.generate_line_art (ComfyUI/FLUX)
                       ─► line_art {raw_mono, width, height}   ◄── device prints
     (any failure) ──► line_art_error {message, stage}
  channel close ─────► flush: if frames buffered and no stop seen, treat as stop
```

## Components

### 1. `app/opus_decode.py`
- `decode_opus_to_wav(frames: list[bytes], sample_rate: int = 16000) -> bytes`
- Decodes raw 16 kHz mono Opus packets to a PCM16 WAV (bytes) using **PyAV**
  (`av`, already installed — no new dependency). The packets are fed to an Opus
  decoder; decoded PCM is written to an in-memory WAV via the `wave` module.
- Isolated and unit-testable; raises a clear error if decoding yields no audio.

### 2. `app/device_protocol.py`
- `async def handle_device_session(ws, first_message: dict) -> None`
  - Sends the hello reply (generated `session_id`, `audio_params` advertising the
    downstream rate; since we send no TTS audio, advertise the device's own
    16000/60 — safe and ignored for our purposes).
  - Loop over frames:
    - text frame: parse JSON; on `listen`/`state:start` reset the Opus buffer and
      mark listening; on `listen`/`state:stop` run the pipeline; other types
      (`hello` repeats, `mcp`, etc.) are logged and ignored.
    - binary frame: while listening, append to the Opus buffer.
  - Pipeline (`_run_line_art`): decode → transcribe → emit `line_art_transcription`
    → `line_art_progress` → generate → `line_art`. On any exception emit
    `line_art_error`. Every outgoing message includes the stored `session_id`.
  - On `WebSocketDisconnect`: if frames are buffered and listening never stopped,
    run the pipeline once as a flush (best-effort), else just return.
- Outgoing helpers build the exact JSON shapes from the contract.

### 3. `app/main.py`
- `/ws` reads the first message. If it is text and parses to JSON with
  `type == "hello"`, delegate to `handle_device_session(ws, parsed)`. Otherwise
  fall through to the existing browser handling (text_input / binary audio),
  unchanged. The existing `handle_text_input` / `handle_audio_input` and the
  `ResultMessage`/`ProgressMessage` browser schemas are untouched.

## Data sent to device (exact shapes)

```jsonc
// server hello reply
{"type":"hello","transport":"websocket","session_id":"<uuid>",
 "audio_params":{"sample_rate":16000,"frame_duration":60}}

{"type":"line_art_transcription","session_id":"<id>","text":"a cat"}
{"type":"line_art_progress","session_id":"<id>","stage":"image_gen",
 "message":"Generating line art for 'a cat'..."}
{"type":"line_art","session_id":"<id>","raw_mono":"<base64 1-bpp>",
 "width":384,"height":384}
{"type":"line_art_error","session_id":"<id>","stage":"stt",
 "message":"Could not transcribe any speech from audio."}
```

`width`/`height` come straight from `generate_line_art` (width fixed 384,
height returned). `raw_mono` is the base64 string already produced today.

## Decisions (locked)

- **Opus → WAV decoded in-app** via PyAV; Speaches unchanged.
- **No TTS audio.** Device output is driven by `line_art_*` (print) messages. No
  spoken Opus playback. (A `tts sentence_start` text could be added later for
  on-screen status; not in scope.)
- **End-of-utterance on `listen` `state:stop`**, with a channel-close flush
  fallback. No silence/VAD timer.
- **Both protocols coexist on `/ws`**, auto-detected by the first message
  (`hello` → device; otherwise → existing browser protocol). The browser test
  client and its tests keep working.

## Non-goals (YAGNI)

- No MCP tool-calling, no RFID `card_content` resolution, no `system`/`alert`
  emission, no binary protocol v2/v3, no TTS audio, no VAD.

## Error handling

- Hello reply sent immediately on receiving the device hello (well under 10 s).
- Keep the device's line-art watchdog alive with `line_art_progress`, and always
  terminate with `line_art` or `line_art_error`.
- Empty transcript → `line_art_error` (stage `stt`). Opus decode failure →
  `line_art_error` (stage `stt`). Image-gen failure → `line_art_error`
  (stage `image_gen`).

## Testing strategy

- **Unit `opus_decode`:** encode a short PCM tone to Opus (PyAV) then decode back;
  assert non-empty PCM16 WAV with the right sample rate. Decode-failure path
  raises.
- **Unit `device_protocol` handshake:** feed a `hello`; assert the reply has
  `type:hello`, `transport:websocket`, a `session_id`, and `audio_params`.
- **Unit `device_protocol` pipeline mapping:** with `stt.transcribe` and
  `image_gen.generate_line_art` mocked, drive listen-start → (binary) →
  listen-stop and assert the emitted sequence is
  `line_art_transcription` → `line_art_progress` → `line_art`, each carrying the
  session_id; and that a raised error yields `line_art_error`.
- **Unit `/ws` dispatch:** first message `hello` routes to the device handler;
  first message `text_input` still routes to the browser handler (existing tests
  stay green).
- **Integration (manual):** a Python client mimicking the device — hello → listen
  start → real Opus frames of "a cat" → listen stop → expect hello +
  line_art_transcription + line_art with a valid 384-wide bitmap. Plus the live
  device on `ws://192.168.0.181:8090/ws`.
