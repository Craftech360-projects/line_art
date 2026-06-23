# Cheeko Device ↔ Server Contract (WebSocket)

What the **AI Printer Cheeko** firmware expects from the realtime server. This is derived directly from the firmware code, not the generic protocol doc:

- Transport / handshake: [main/protocols/websocket_protocol.cc](../main/protocols/websocket_protocol.cc)
- Incoming JSON dispatch: [main/application.cc:576-749](../main/application.cc#L576-L749)
- Card content / skill download: [main/boards/common/content_manager.cc](../main/boards/common/content_manager.cc)

Configured endpoint: `ws://192.168.0.181:8090/ws` (see `CHEEKO_WEBSOCKET_URL` in [main/application.cc:512](../main/application.cc#L512)).

---

## 1. Connection

The device opens **one** WebSocket connection when a voice session starts. On the upgrade request it sends these headers ([websocket_protocol.cc:101-110](../main/protocols/websocket_protocol.cc#L101-L110)):

| Header | Value |
|---|---|
| `Authorization` | `Bearer <token>` — only if a token is configured; omitted otherwise |
| `Protocol-Version` | `1` (the binary protocol version the device uses) |
| `Device-Id` | device MAC address |
| `Client-Id` | firmware-generated UUID |

The server must accept the upgrade and then complete the hello handshake **within 10 seconds**, or the device aborts with a timeout error ([websocket_protocol.cc:191-196](../main/protocols/websocket_protocol.cc#L191-L196)).

---

## 2. Handshake

### Device → server (`hello`, sent first)
```json
{
  "type": "hello",
  "version": 1,
  "features": { "mcp": true },
  "transport": "websocket",
  "audio_params": {
    "format": "opus",
    "sample_rate": 16000,
    "channels": 1,
    "frame_duration": 60
  }
}
```

### Server → device (`hello`, required reply)
The device **requires** this message to consider the channel open. It must:
- have `"type": "hello"`
- have `"transport": "websocket"` — any other value is rejected ([websocket_protocol.cc:231-235](../main/protocols/websocket_protocol.cc#L231-L235))

```json
{
  "type": "hello",
  "transport": "websocket",
  "session_id": "abc123",
  "audio_params": {
    "sample_rate": 24000,
    "frame_duration": 60
  }
}
```

- `session_id` — optional; if present the device stores it and echoes it on outgoing messages.
- `audio_params.sample_rate` / `frame_duration` — optional; tell the device the rate of the **downstream** audio you will send. The device resamples to its codec rate. If omitted it keeps its default. **Send what you actually encode at** (e.g. 24000 for nicer TTS).

---

## 3. Audio (binary frames)

- Codec: **Opus**, mono.
- Protocol version 1 → the device sends/receives **raw Opus payloads** as binary WebSocket frames, no header ([websocket_protocol.cc:55-57](../main/protocols/websocket_protocol.cc#L55-L57), [139-146](../main/protocols/websocket_protocol.cc#L139-L146)). (Versions 2/3 with a binary header exist but are not used here.)
- Uplink (mic): 16 kHz, 60 ms frames.
- Downlink (TTS/playback): whatever `sample_rate` you advertised in the server hello; the device resamples.
- Frames received while the device is in the listening state may be dropped to avoid feedback.

---

## 4. Server → device JSON messages

Every message is a text frame with a `type` field. Handlers in [application.cc:576-749](../main/application.cc#L576-L749). Unknown types are logged and ignored.

### Standard voice-assistant types

| `type` | Fields | Effect on device |
|---|---|---|
| `tts` | `state: "start"` | Enter **Speaking** state; play following audio frames |
| `tts` | `state: "stop"` | Stop speaking → back to Listening or Idle |
| `tts` | `state: "sentence_start"`, `text` | Show assistant text on display |
| `stt` | `text` | Show recognized user text on display |
| `llm` | `emotion` (string) | Set the emotion/expression on the display |
| `mcp` | `payload` (JSON-RPC 2.0 object) | Routed to the MCP server for tool calls (IoT control) |
| `system` | `command: "reboot"` | Reboots the device. Other commands logged as unknown |
| `alert` | `status`, `message`, `emotion` (all strings) | Shows an alert + plays a vibration sound. All three required |
| `mode_update` | `listening_mode: "manual"｜"realtime"｜"auto"` | Switches listening mode (anything else = auto) |
| `agent_ready` | — | Logged only |
| `ready` | optional `board_type` (string) | Logged; marks session ready |

> Note: there is **no** `custom` handler in this firmware (the generic doc lists one, but Cheeko does not compile it). Use the typed messages below instead.

### Cheeko-specific: line art / printing

The printer pipeline streams progress then a final monochrome bitmap.

| `type` | Fields | Effect |
|---|---|---|
| `line_art_transcription` | `text` | Shows the user's prompt; refreshes the line-art timeout |
| `line_art_progress` | `message` (required), `stage` (optional: `"input"`, `"stt"`, `"image_gen"`) | Notification + system chat line; refreshes timeout |
| `line_art_error` | `message` (required), `stage` (optional) | Notification + system chat line; cancels timeout |
| `line_art` | `raw_mono` (required string), `width` (int), `height` (int) | Final image to print. Cancels timeout and hands `raw_mono`+dims to the display/printer |

`line_art` example:
```json
{
  "type": "line_art",
  "raw_mono": "<base64 / packed 1-bpp bitmap>",
  "width": 384,
  "height": 240
}
```
Send the `*_progress` updates first, then exactly one `line_art` (or a `line_art_error`) to release the device from its waiting state.

### Cheeko-specific: print confirmation (device → server) — generation is gated

Image generation is gated on the user's confirmation. After the server sends
`line_art_transcription`, the device asks the user "print this?" and the server
**must wait** — it must NOT generate the bitmap yet. The device then sends
exactly one decision (text frame):

| `type` | Meaning | Server must |
|---|---|---|
| `print_confirm` | user accepted the transcription | generate, then send `line_art_progress` + exactly one `line_art` (or `line_art_error`) |
| `print_reject` | user rejected | abort this prompt; send nothing further |

These carry no payload beyond `type`; the server correlates them with the most
recent `line_art_transcription` on the connection (one prompt in flight at a time).

Gated sequence:

```
device → server : (listen start / opus frames / listen stop)
server → device : line_art_transcription { text }
                  ── server PAUSES; no generation yet ──
device          : shows "print this? RECORD = print · CANCEL = reject"

  print_confirm → server generates → line_art_progress → line_art (or line_art_error)
  print_reject  → server aborts, sends nothing
```

Notes:
- After `print_confirm` the device waits in a DRAWING state with no client-side
  timeout, so the server must always terminate with `line_art` or `line_art_error`.
- A new audio upload (`listen start`) voids any prior un-confirmed transcription.
- A `print_confirm` with no pending transcription is ignored.

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

### Cheeko-specific: RFID card content

When the device scans an RFID card it asks the server to resolve it. The server replies with one of:

**Unknown card** ([content_manager.cc:101-107](../main/boards/common/content_manager.cc#L101-L107)):
```json
{ "type": "card_unknown", "rfid_uid": "04A1B2C3" }
```

**Known card** ([content_manager.cc:109-168](../main/boards/common/content_manager.cc#L109-L168)):
```json
{
  "type": "card_content",
  "rfid_uid": "04A1B2C3",
  "skill_id": "mo700623",
  "skill_name": "Dinosaurs",
  "version": 1,
  "audio":  [ { "index": 0, "url": "https://.../001.mp3" } ],
  "images": [ { "index": 0, "url": "https://.../001.png" } ]
}
```

Field requirements:
- `rfid_uid` — **required**, string.
- `skill_id` — **required**, string. Lowercased by the device (FAT32 filenames are lowercased too).
- `skill_name` — optional; defaults to `skill_id`.
- `version` — optional number; defaults to `1`.
- `audio` / `images` — arrays of `{ "index": <int>, "url": <string> }`. The device **downloads each URL** to the SD card unless the skill is already present. URLs must be directly fetchable by the device.

---

## 5. Timing / error behavior the server should respect

- **Hello timeout:** server hello must arrive < 10 s after connect, or the device drops the channel.
- **Line-art watchdog:** the device runs a timeout while waiting for `line_art`. Keep it alive with `line_art_progress`, and always terminate with `line_art` or `line_art_error`.
- **Disconnect:** if the socket drops, the device closes the audio channel and returns to Idle. Reconnection is driven by the device on the next session.
- **Card downloads are blocking:** the device blocks on downloading `audio`/`images` URLs; keep them reasonably sized and reachable.
- Missing/invalid required fields cause the message to be logged and ignored — no error is sent back to the server.
