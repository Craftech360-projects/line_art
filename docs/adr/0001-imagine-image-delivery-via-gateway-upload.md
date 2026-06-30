# 1. AI Imagine image delivery: line_art returns bytes, gateway uploads to S3

Date: 2026-06-30

## Status

Accepted

## Context

AI Imagine speaks a prompt and displays a generated **color image** on the device LCD.
The device fetches the image as a **public HTTPS URL** (it has a hardware JPEG decoder and
already does HTTPS downloads); it cannot receive large inline payloads over its MQTT control
plane. See [CONTEXT.md](../../CONTEXT.md) and `ai-imagine-backend-spec.md`.

The server-side pipeline is the **gateway shortcut**: device → `cheeko-backend/mqtt-gateway`
→ line_art (STT + image generation), bypassing LiveKit/picoclaw. line_art already owns the
Whisper STT + FLUX/ComfyUI generation pipeline, but runs on the LAN, writes images to a local
folder, and has no cloud credentials. cheeko-backend's `manager-api-node` already has the AWS
S3 SDK and an upload service.

The question: who turns a generated image into a public URL?

## Decision

Split responsibilities by what each component already owns:

1. **line_art** generates a color JPEG (≤320×240, ≤~200 KB, baseline) and returns the
   **bytes** to the gateway over its existing WebSocket session. line_art does **not** touch
   S3 and gains no cloud credentials. It stays a pure "audio in → image bytes out" service.
2. **gateway** receives the bytes and POSTs them to a **dedicated internal upload endpoint**
   on manager-api (service-key auth, content-type/size locked to JPEG). manager-api uploads
   to **AWS S3** under an `imagine/` prefix with a **random, unguessable key** and returns the
   **public** `https://cdn.cheekoai.in/...` URL (no per-request auth — the device GETs it
   directly).
3. **gateway** publishes the device-facing `image{url}` message over MQTT.

## Consequences

- Cloud credentials and the device-facing MQTT contract live in **one place** (the gateway +
  manager-api), not spread into the Python service.
- line_art remains LAN-only, stateless, and free of AWS/boto3 — the printer path is untouched.
- The image makes an extra hop (line_art → gateway → manager-api → S3) versus line_art
  uploading directly. Accepted: hop is on a fast internal network and keeps responsibilities
  clean.
- URLs are public-but-unguessable. If leakage of children's generated images is later a
  concern, add short-TTL objects or signed URLs — a reversible change behind the same contract.
- A new dedicated upload endpoint must enforce the device's JPEG/size constraints server-side.
