# AI Imagine — Firmware Implementation Notes

> For the **device firmware** developer. Companion to `ai-imagine-backend-spec.md`
> (the wire contract). This document covers the device-side behavior the server now
> depends on. Server side (gateway + line_art + manager-api) is implemented and live.

## TL;DR of required changes

1. **Wait up to 90 s for the image** — image generation is slow (STT + diffusion).
2. **Show a "still imagining…" screen** driven by `image_status`.
3. **Do NOT send `goodbye` while waiting** for an image.
4. **Ignore messages whose `session_id` ≠ the current session.**

The single most important one is **#1** — today the device gives up in ~15–20 s and
sends `goodbye`, so the image never displays even though the server produced it.

---

## The flow (device ⇄ server)

```
Device → Server : hello { feature: "ai_imagine" }          (session-level flag, Option A)
Server → Device : hello { session_id, udp:{…} }
Device → Server : listen { state:"start", mode:"manual" }   (knob pressed)
Device → Server : <Opus audio frames over UDP>              (child speaks)
Device → Server : speech_end                                (knob released)  ← the trigger
Server → Device : image_status { state:"generating" }       (optional, ~immediately)
Server → Device : image { url } | image_error { code }      (seconds later)
Device          : HTTPS GET url → decode JPEG → full-screen
```

## 1. Timeout — wait up to 90 s

- Server default generation budget is **90 s** (`IMAGINE_TIMEOUT_MS`, gateway env).
- **Device timeout must be ≥ the server budget.** Recommended: **90 s** hard cap after
  `speech_end`, cancellable by the user (e.g. knob press to abort).
- Previously the device timed out ~15–20 s and sent `goodbye`; the server then dropped
  the finished image because the session was gone. Raising this is the key fix.
- The server is also being tuned to generate faster (smaller image), but the device
  must still tolerate the worst case.

## 2. Progress UI — `image_status`

```json
{ "type": "image_status", "session_id": "…", "request_id": "img_ab12cd34", "state": "generating" }
```
- On receipt, show a friendly **"still imagining…"** animation/screen.
- `state` may be `queued` | `generating` | `uploading` (currently only `generating` is sent).
- Treat `image_status` as "keep waiting, reset any short idle timer." Do **not** require it —
  if it never arrives, still wait the full 90 s.

## 3. Success — `image`

```json
{
  "type": "image", "session_id": "…", "request_id": "img_ab12cd34",
  "url": "https://…cloudfront.net/imagine/<uuid>.jpg",
  "mime": "image/jpeg", "width": 320, "height": 240, "caption": "a beautiful cat"
}
```
- **HTTPS** GET the `url` (plain HTTP is not served). Decode baseline JPEG, ~≤200 KB,
  ≤320×240, 24-bit RGB → display full-screen.
- `caption` is optional short text to show under/over the image.
- The URL is public but unguessable and may expire (lifecycle policy) — **fetch promptly**.

## 4. Failure — `image_error`

```json
{ "type": "image_error", "session_id": "…", "request_id": "img_ab12cd34",
  "code": "no_speech", "message": "…" }
```
Show a friendly, child-appropriate retry message per `code`:

| `code` | Meaning | Suggested on-device copy |
|--------|---------|--------------------------|
| `no_speech` | No speech captured | "I didn't hear you — try again!" |
| `safety_block` | Prompt not allowed for kids | "Let's imagine something else!" |
| `generation_failed` | Model/internal/timeout | "Hmm, that didn't work. Try again!" |
| `rate_limited` | Too many requests | "One at a time! Try again in a moment." |

## 5. Don't send `goodbye` while waiting

- The device must **not** tear down / send `goodbye` between `speech_end` and the
  terminal `image` / `image_error`. Doing so makes the server discard the result.
- `goodbye` is for leaving the AI-Imagine menu entirely.

## 6. Session correlation

- Every server→device message carries `session_id`. **Ignore** any `image` /
  `image_status` / `image_error` whose `session_id` does not match the device's
  current session (guards against a late result from a previous/aborted session after
  a reconnect).
- `request_id` correlates a response to its utterance; useful in device logs.

## 7. One image at a time

- The server serializes: one image per session. A new `speech_end` while an image is
  still generating is ignored server-side. The device should likewise disable the
  capture control until the current `image`/`image_error` arrives (or the 90 s timeout).

---

## Quick server-side reference (already implemented)

- Trigger: `speech_end` (the device sends it on knob release). `listen{state:"stop"}` also works.
- `feature:"ai_imagine"` goes in the **hello** (session-level).
- Errors and timeouts always resolve to an `image_error` — the device is never left
  waiting with no terminal message (other than its own 90 s cap).
