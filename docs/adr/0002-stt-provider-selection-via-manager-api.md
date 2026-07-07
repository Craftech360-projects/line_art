# 2. STT provider selection: manager-api active provider + env last-resort fallback

Date: 2026-07-07

## Status

Accepted

## Context

line_art transcribes device/browser audio through a single STT backend chosen at
startup by an env flag (`STT_BACKEND` = `groq` | `local`). Going to production we want
(a) vendor flexibility — swap the active STT provider without redeploying line_art —
and (b) failover, so one provider outage doesn't take the feature down. We explicitly
do **not** want language-based routing here (a separate concern, deferred).

cheeko-backend's `manager-api-node` already owns provider configuration: an
`stt_providers` table (seeded with `deepgram`, `groq`, `sarvam`, …) with exactly **one**
`is_active` row at a time, admin-managed, exposed at `GET /providers/active` →
`{ provider, model, language, api_key }`. That row is **shared** with the picoclaw
LiveKit voice agent — the table was built for it (`sample_rate`, `config_json` for
streaming). The route is behind `requireAdmin`, which also accepts a service key
(`X-Service-Key: $SERVICE_SECRET_KEY`) as internal-tool "god mode".

The question: where does line_art get its STT provider(s), and how does it stay up when
that source or a provider is unavailable?

## Decision

1. **Primary provider = the manager-api active row.** line_art calls
   `GET /providers/active` with the service key and uses the returned
   `{ provider, model, language, api_key }`. It does **not** hold per-provider keys for
   the primary — the key comes from the API response.
2. **Cache with TTL + last-known-good.** Fetched on first use and refreshed on a ~5-min
   TTL, off the request hot path. Admin changes propagate within the TTL.
3. **Env last-resort is the single failure sink.** A fixed, self-contained provider
   configured in line_art env (Groq, `GROQ_API_KEY`) is used whenever the primary can't
   serve: primary hard-failure, manager-api down / cold cache, or an active provider
   line_art has no adapter for.
4. **Fallback triggers on hard failures only** — connect error, timeout, HTTP 5xx, 429,
   auth. A `200` with empty/garbage text is accepted as "no speech" (not a fallback
   trigger); the existing `MIN_UTTERANCE_FRAMES` silence guard remains the backstop.
5. **Chain depth ≤ 2, 30 s per provider.** Primary then last-resort, nothing deeper, so
   the worst case (60 s STT + 20 s image-gen = 80 s) stays under the gateway's ~90 s
   `IMAGINE_TIMEOUT`.
6. **Unknown provider → last-resort.** An active provider with no line_art adapter logs a
   warning and falls to the env last-resort, so adapters can be added incrementally
   without a coordinated release.

v1 adapters: Groq (exists), Deepgram, Sarvam.

## Consequences

- Provider config lives in one admin-controlled place; line_art needs no redeploy to
  switch the primary. It does need `SERVICE_SECRET_KEY` and the env last-resort creds.
- The active row is **shared with the voice agent**: a voice-chat provider toggle also
  moves line_art's STT. Accepted as a single STT knob for now; split into a separate row
  if the two features ever need different providers.
- Regional-language misses are **not** fixed here — if the active provider returns empty
  on, say, Hindi, we treat it as no-speech and stop rather than trying a
  language-appropriate provider. That's the deferred routing concern.
- The seeded Sarvam model `saaras:v3` is speech-to-**translation** (outputs English),
  which happens to suit English FLUX prompts; switching to Saarika (source-language
  transcription) is an admin/DB choice, not a line_art change.
- The active-provider API is now a soft dependency in line_art's path; the TTL cache +
  last-known-good + env last-resort keep an outage from blocking transcription.
