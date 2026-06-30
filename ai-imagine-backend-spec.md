# AI Imagine — Server Communication Specification

> **Status: PROPOSAL — for server-team validation.**
> The device firmware for this feature is **not yet built**. This document defines the
> device⇄server contract we intend to implement so the server team can validate /
> adjust it before any code is written. Sections marked **(existing)** already work today
> in AI Chat and are unchanged; sections marked **(NEW)** are what AI Imagine adds.

## Overview

**AI Imagine** is a new menu option on the Cheeko V2 device. The child presses the knob
and speaks a prompt (e.g. *"a blue dog surfing a wave"*); the device streams that speech
to the server exactly as it does for AI Chat. Instead of replying with TTS audio, the
server generates an **image** and returns it, which the device displays full-screen.

The key design rule: **AI Imagine reuses the entire AI-Chat real-time voice pipeline
unchanged.** The only differences are:
1. a **differentiating tag** (`feature: "ai_imagine"`) so the server routes the utterance
   to its image pipeline instead of the chat LLM/TTS, and
2. a **new `image` response message** carrying the generated picture.

```
┌─────────────┐   MQTT (JSON control)  +  UDP (Opus audio, AES-128-CTR)   ┌─────────────┐
│   SERVER    │ ◄──────────────────────────────────────────────────────► │   DEVICE    │
│             │                                                           │             │
│  STT        │   ◄── voice (Opus frames) ──                              │ Microphone  │
│  Image-gen  │   ◄── listen/start {feature:"ai_imagine"} ──              │ Rotary knob │
│  CDN host   │   ── image {url} ──►                                      │ LVGL + LCD  │
└─────────────┘                                                           └─────────────┘
                                        device downloads url over HTTPS, decodes, displays
```

---

## 1. Transport & session  **(existing)**

AI Imagine uses the **same transport and session model as AI Chat**. No changes here — it
is documented so the server team can see where the new fields slot in.

- **Control plane:** JSON text messages over **MQTT** (topic `devices/p2p/<client_id>`).
- **Audio plane:** **Opus** frames over **UDP**, encrypted `aes-128-ctr`.
- (A WebSocket transport also exists with the same JSON message shapes; this spec uses the
  MQTT+UDP path since that is what production devices use.)

### 1.1 Session handshake

**Device → Server — `hello`** (device requests an audio channel):
```json
{
  "type": "hello",
  "version": 3,
  "transport": "udp",
  "features": { "aec": true, "mcp": true },
  "audio_params": { "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60 }
}
```

**Server → Device — `hello`** (grants channel, opens session):
```json
{
  "type": "hello",
  "version": 3,
  "mode": "conversation",
  "session_id": "ed35433e-…_9888E0066F50_conversation",
  "timestamp": 1782738699897,
  "transport": "udp",
  "udp": {
    "server": "139.59.7.72",
    "port": 8884,
    "encryption": "aes-128-ctr",
    "key": "d856c7fa16ca45fbce6d297981c35271",
    "nonce": "010000005495ea740000000000000000",
    "connection_id": 1419111028,
    "cookie": 1419111028
  },
  "audio_params": { "sample_rate": 24000, "channels": 1, "frame_duration": 60, "format": "opus" }
}
```

- `session_id` from the server is echoed on **every** subsequent device→server message.
- Device mic audio is `16000 Hz`; server audio (TTS) is `24000 Hz`. For AI Imagine there is
  no TTS, so the `24000` return audio is unused (unless the server also wants to speak — see
  Open Question 4).

---

## 2. The differentiating tag  **(NEW)**

The server must be able to tell an Imagine utterance from a normal chat utterance. We
propose the tag `"feature": "ai_imagine"`. Two placement options — **the server team should
pick one** (we can implement either):

### Option A — per **session** (recommended)
Add `feature` to the device `hello`. The whole session is image-mode; every utterance in it
is an image request.
```json
{
  "type": "hello",
  "version": 3,
  "transport": "udp",
  "feature": "ai_imagine",
  "features": { "aec": true, "mcp": true },
  "audio_params": { "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60 }
}
```
*Pros:* clean separation; server can pick a different `mode` in its hello reply (e.g.
`"mode": "imagine"`); natural if the child makes several images in a row.

### Option B — per **utterance**
Add `feature` to each `listen`/`start` message (the session stays a normal conversation
session). See §3.2.
*Pros:* one session can mix chat and imagine. *Cons:* server must branch per utterance.

> **We recommend Option A.** Default to `feature: "ai_imagine"` at the session level.

---

## 3. Device → Server messages

### 3.1 `hello` — see §1.1 / §2.

### 3.2 `listen` / `start` — begin capturing speech  **(field is NEW)**
Sent when the child presses the knob to talk.

**Existing (AI Chat):**
```json
{ "session_id": "…", "type": "listen", "state": "start", "mode": "manual" }
```

**AI Imagine (Option B placement):**
```json
{ "session_id": "…", "type": "listen", "state": "start", "mode": "manual", "feature": "ai_imagine" }
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | From server `hello` |
| `type` | string | yes | `"listen"` |
| `state` | string | yes | `"start"` |
| `mode` | string | yes | `"manual"` for Cheeko (knob press-to-talk) |
| `feature` | string | **NEW** | `"ai_imagine"` — only if using Option B (omit for Option A) |

### 3.3 Audio frames — `Opus`  **(existing)**
After `listen/start`, the device streams Opus frames over the UDP channel (encrypted with
the `key`/`nonce` from the server hello). Frame cadence = `frame_duration` (60 ms), mono,
16 kHz. **Unchanged from AI Chat** — the server's existing decode/STT applies.

### 3.4 `speech_end` — child finished speaking  **(existing)**
Sent when the child releases / presses the knob to end.
```json
{ "session_id": "…", "type": "speech_end" }
```

### 3.5 `listen` / `stop`, `abort`, `goodbye`  **(existing)**
Standard teardown/cancel messages, unchanged:
```json
{ "session_id": "…", "type": "listen", "state": "stop" }
{ "session_id": "…", "type": "abort" }
{ "session_id": "…", "type": "goodbye" }
```

---

## 4. Server → Device messages

### 4.1 `stt` — transcription (optional, existing)
If the server emits `stt`, the device can show the recognized prompt text while the image
generates. **Reused as-is.**
```json
{ "type": "stt", "session_id": "…", "text": "a blue dog surfing a wave" }
```

### 4.2 `image` — the generated picture  **(NEW — core of this feature)**
The server's response to an Imagine utterance. **URL-based delivery** (see §5 for rationale).
```json
{
  "type": "image",
  "session_id": "ed35433e-…_conversation",
  "request_id": "img_7af31c20",
  "url": "https://cdn.cheekoai.in/imagine/img_7af31c20.jpg",
  "mime": "image/jpeg",
  "width": 320,
  "height": 240,
  "caption": "a blue dog surfing a wave",
  "expires_at": 1782739999
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | yes | `"image"` |
| `session_id` | string | yes | Must match the active session |
| `request_id` | string | recommended | Correlates the response to the utterance; shown in device logs |
| `url` | string | yes | **HTTPS** URL the device GETs and displays |
| `mime` | string | recommended | `"image/jpeg"` or `"image/png"` (see §5 constraints) |
| `width` / `height` | number | recommended | Pixel size; lets device pre-allocate / decide scaling |
| `caption` | string | optional | Short text shown under/over the image |
| `expires_at` | number | optional | Unix seconds; device should fetch promptly |

### 4.3 `image_status` — progress / generating (optional, NEW)
Image generation can take several seconds. An optional progress ping lets the device show a
"still imagining…" state instead of timing out.
```json
{ "type": "image_status", "session_id": "…", "request_id": "img_7af31c20", "state": "generating" }
```
`state` ∈ `"queued" | "generating" | "uploading"`. **If the server cannot send this, the
device falls back to a fixed client-side timeout (Open Question 3).**

### 4.4 `image_error` — generation failed (NEW)
```json
{ "type": "image_error", "session_id": "…", "request_id": "img_7af31c20", "code": "safety_block", "message": "Could not create that picture." }
```
| `code` (suggested) | Meaning |
|--------------------|---------|
| `safety_block` | Prompt/content filtered |
| `no_speech` | STT found no usable prompt |
| `generation_failed` | Model/internal error |
| `rate_limited` | Too many requests |

The device shows a friendly retry message on any `image_error`.

---

## 5. Image delivery details  **(NEW)**

We strongly prefer **URL download over inline bytes**:

- The device already performs HTTPS downloads (OTA, content packs) and has a hardware
  **JPEG decoder** plus ~7 MB free PSRAM for the decoded bitmap.
- The control channel is **MQTT**, which is a poor fit for large binary payloads; audio is on
  UDP. Inline base64 in JSON would bloat the broker path. (If the server *strongly* prefers
  inline, we can support base64 over WebSocket — but URL is the recommended path.)

**Image requirements (please confirm / adjust):**

| Constraint | Proposed value | Notes |
|------------|----------------|-------|
| Transport | **HTTPS** | Plain HTTP not accepted on device |
| Format | **JPEG (baseline)** preferred; PNG acceptable | Progressive JPEG **not** supported by the decoder |
| Dimensions | **≤ 320 × 240** (device LCD is 320×240 landscape) | Larger images are downscaled by the device; sending pre-sized saves RAM/time |
| File size | **≤ ~200 KB** target | Keeps download < ~1–2 s on device Wi-Fi |
| Auth | TBD | Does the URL need an auth header/token, or is it a signed/public URL? (Open Question 2) |
| Lifetime | URL valid ≥ 60 s after `image` sent | Device fetches immediately on receipt |
| Color | 24-bit RGB | Device renders RGB565 |

---

## 6. End-to-end flow

```
1. Child selects "IMAGINE" in the menu.
2. Device → Server : hello { feature:"ai_imagine" }            (Option A)
3. Server → Device : hello { session_id, udp:{…} }
4. Child presses knob → Device → Server : listen/start          (+feature if Option B)
5. Child speaks      → Device → Server : Opus audio frames (UDP)
6. Child releases    → Device → Server : speech_end
7. Server: STT → image generation
   (optional) Server → Device : stt { text }
   (optional) Server → Device : image_status { generating }
8. Server → Device : image { url }
9. Device GETs url over HTTPS → decodes JPEG → displays full-screen + caption.
10. Child presses knob → go to step 4 for another image, or backs out to the menu.
```

---

## 7. Open questions for the server team

1. **Tag placement:** Option A (session-level `hello.feature`) or Option B (per-utterance
   `listen.start.feature`)? We recommend **A**. Also confirm the exact value string
   (`"ai_imagine"` vs `"imagine"` vs an `"app"` field).
2. **Image URL auth:** public/signed URL, or does the device need to send an `Authorization`
   header / token to fetch it?
3. **Latency & progress:** typical and worst-case generation time? Will the server send
   `image_status`, or should the device use a fixed timeout (and what value)?
4. **Audio reply:** for AI Imagine, will the server *also* speak (TTS, e.g. "Here's your blue
   dog!"), or is the response image-only? This decides whether the device keeps the speaker
   path active.
5. **Multiple/rapid requests:** how should the server handle a new utterance arriving before
   the previous image is delivered — cancel, queue, or reject?
6. **Format confirmation:** can the CDN serve baseline JPEG ≤ 320×240 ≤ ~200 KB over HTTPS?
7. **Reuse vs. new message type:** is a dedicated `"image"` type acceptable, or does the
   server prefer to wrap it in the existing generic `"custom"` message type?

---

## Appendix A — Existing message types (reference, unchanged)

For context, the current AI-Chat session uses these server→device types (device dispatch in
`main/application.cc`): `tts` (TTS audio start/stop/sentence), `stt`, `llm` (emotion/text),
`mcp`, `system`, `alert`, `custom`, `agent_ready`, and `card_*` (RFID card responses).
AI Imagine adds `image`, `image_status`, and `image_error`, and reuses `stt`/`agent_ready`.

## Appendix B — Field naming summary (new fields only)

| Message | New field | Value |
|---------|-----------|-------|
| `hello` (Option A) | `feature` | `"ai_imagine"` |
| `listen`/`start` (Option B) | `feature` | `"ai_imagine"` |
| `image` (server→device) | whole message | see §4.2 |
| `image_status` (server→device) | whole message | see §4.3 |
| `image_error` (server→device) | whole message | see §4.4 |
