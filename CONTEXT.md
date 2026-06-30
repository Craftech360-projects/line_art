# Context: AI Imagine

Glossary for the AI Imagine feature. Terms only — no implementation detail.

## Components

- **Device** — the Cheeko V2 toy. Speaks the xiaozhi wire protocol: MQTT for JSON
  control messages, UDP for Opus audio. Has a 320×240 color LCD and a hardware JPEG decoder.
- **mqtt-gateway** — `cheeko-backend/mqtt-gateway`. The server endpoint the Device connects
  to. Terminates the MQTT control plane and the UDP audio plane, and decodes Opus→PCM. For
  AI Chat it bridges that audio into a LiveKit room; for **AI Imagine** it does *not* bridge —
  it takes the gateway shortcut.
- **LiveKit room** — the real-time audio room used by AI Chat. Not involved in AI Imagine.
- **picoclaw agent** — `D:\picoclaw`. The Go AI agent that joins the LiveKit room and runs
  STT→LLM→TTS for **AI Chat**. Not involved in AI Imagine.
- **line_art** — `D:\line_art`. Python/FastAPI service that already owns the voice→STT→
  image-generation pipeline (Whisper + FLUX/ComfyUI). Originally built for the **AI Printer**
  (1-bit monochrome bitmaps). AI Imagine reuses its STT + image generation.

## Features

- **AI Chat** — the existing real-time voice conversation. Device → gateway → LiveKit →
  picoclaw → TTS audio back. Unchanged.
- **AI Printer** — the existing line_art feature: speak a prompt, generate a 1-bit line-art
  bitmap, gated on `print_confirm`, sent to a thermal printer.
- **AI Imagine** — the NEW feature. Speak a prompt, generate a **color image**, return it as
  an `image` message carrying a CDN **URL**; the Device displays it full-screen on the LCD.
  No TTS.

## Routing

- **Gateway shortcut** — the chosen AI Imagine routing: when the gateway sees
  `feature: "ai_imagine"`, it forwards the raw Opus audio into line_art's existing WebSocket
  device protocol (STT + image-gen) and relays the result back to the Device as an `image`
  message, bypassing LiveKit and picoclaw entirely.
- **Imagine mode** — line_art's branch (vs the AI Printer path) when a session's `hello`
  carries `feature: "ai_imagine"`: generate immediately (no `print_confirm` gate), produce a
  color JPEG instead of a 1-bit bitmap, return the bytes to the gateway.

## Device wire messages (server → device)

- **`image`** — the generated picture, carrying a public CDN **URL** the Device GETs over
  HTTPS. The terminal AI Imagine response.
- **`image_status`** — optional progress ping (`queued`/`generating`/`uploading`) so the
  Device shows a "still imagining…" state instead of timing out.
- **`image_error`** — generation failed. Codes: `no_speech`, `safety_block`,
  `generation_failed`, `rate_limited`.

These are emitted by the **gateway** (which owns the Device's MQTT session); line_art never
speaks MQTT.
