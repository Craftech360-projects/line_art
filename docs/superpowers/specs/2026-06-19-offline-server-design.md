# Design: Fully Offline Line Art Server

**Date:** 2026-06-19
**Status:** Approved

## Goal

Convert the Line Art Generator from a server that depends on two external cloud
services (Groq Whisper for speech-to-text, HuggingFace FLUX.1 for image
generation) into a **fully offline server**. After this change the application
runs with no internet access and no API keys, calling only local services on the
same PC.

## Non-Goals (YAGNI)

- No cloud fallback or `local|cloud` mode toggle. The cloud code is removed.
- No change to the WebSocket protocol or the `raw_mono` 1-bit bitmap output
  format. Clients/devices see identical behavior.
- No containerizing ComfyUI. It runs natively on Windows for direct RTX 4090
  access (Docker GPU passthrough on Windows is brittle).

## Target Environment

- Windows 11, RTX 4090 (24 GB VRAM), 64 GB RAM.
- Docker 28.x installed (used for Speaches).
- Python 3.11 for the FastAPI app.
- ComfyUI installed natively with FLUX.1-schnell **fp8** model.

## Architecture

```
                  +--------------------------------------+
   Browser/IoT    |   FastAPI app  (port 8000)           |
   --ws://:8000-->|   app/main.py  -- WebSocket /ws       |
                  +------+------------------+-------------+
                         | httpx            | httpx
              +----------v------+   +--------v--------------+
              | Speaches (STT)  |   | ComfyUI (image gen)   |
              | Docker :8001    |   | native Win :8188      |
              | whisper-large-v3|   | FLUX.1-schnell fp8    |
              +-----------------+   | RTX 4090, ~2-4s/img   |
                                    +-----------------------+
```

### Port plan

| Service   | Port | Notes                                              |
|-----------|------|----------------------------------------------------|
| FastAPI   | 8000 | Unchanged.                                         |
| Speaches  | 8001 | Moved off 8000 (its default) to avoid clash. Host port maps to container's 8000. |
| ComfyUI   | 8188 | ComfyUI default.                                   |

All three URLs are configurable via `.env`.

## Components and Changes

### 1. `app/stt.py` — Speaches instead of Groq

Speaches exposes an OpenAI-compatible endpoint, the same shape Groq used.

- POST to `{SPEACHES_BASE_URL}/v1/audio/transcriptions`.
- Multipart form: `file` = WAV bytes (`audio.wav`, `audio/wav`), `model` =
  `{SPEACHES_MODEL}`, `response_format` = `json`.
- **Remove** the `Authorization` header and the `GROQ_API_KEY` check.
- Read `SPEACHES_BASE_URL` and `SPEACHES_MODEL` from env (with sensible
  defaults). Keep the existing async httpx flow and the `result.get("text")`
  parsing — Speaches returns `{"text": "..."}` like Groq.
- On connection failure (service down), raise a clear error so the caller can
  surface a "Speaches unavailable" WebSocket error.

### 2. `app/image_gen.py` — ComfyUI instead of HuggingFace

Only the **source of the PNG bytes** changes. The 1-bit conversion
(`to_raw_mono`) and the prompt template (`build_prompt`) are **unchanged**, so
the device-facing output is byte-for-byte equivalent.

New generation flow against ComfyUI's HTTP API:

1. Build a FLUX.1-schnell workflow graph (JSON) parameterized with the prompt.
   The workflow uses the standard FLUX schnell nodes (checkpoint/UNet loader,
   CLIP, empty latent, KSampler at 4 steps, VAE decode, SaveImage) sized for a
   landscape/portrait output that the resizer will normalize to 384px wide.
2. POST the graph to `{COMFYUI_BASE_URL}/prompt` with a `client_id`; receive a
   `prompt_id`.
3. Poll `{COMFYUI_BASE_URL}/history/{prompt_id}` until the output image appears
   (bounded timeout, e.g. 120 s).
4. Fetch the PNG bytes via `{COMFYUI_BASE_URL}/view?filename=...&subfolder=...&type=output`.
5. Pass those bytes into the existing `to_raw_mono` → return the same
   `(data-uri, prompt_used, raw_mono_b64, height)` tuple.

- Replace `generate_with_huggingface` with `generate_with_comfyui`.
- `generate_line_art` keeps its signature shape but drops the `hf_token` param
  (no token needed). Reads `COMFYUI_BASE_URL` from env.
- On connection/timeout failure, raise a clear error.

The exact ComfyUI workflow JSON and the polling helper are implementation
details for the plan; the contract above is fixed.

### 3. `app/main.py` — startup + wiring

- Remove `HF_TOKEN` and `GROQ_API_KEY` references.
- `lifespan`: log the configured local service URLs instead of API-key warnings.
  Optionally attempt a non-fatal health ping to Speaches/ComfyUI and log
  reachability (does not block startup).
- `handle_text_input` no longer passes `HF_TOKEN` to `generate_line_art`.
- Error messages stay in the existing shape:
  `{"type":"error","stage":"stt"|"image_gen","message":"... service unavailable, start <X>"}`.

### 4. Config

`.env` / `.env.example` become:

```
# Local Speaches (speech-to-text) server
SPEACHES_BASE_URL=http://localhost:8001
SPEACHES_MODEL=Systran/faster-whisper-large-v3

# Local ComfyUI (image generation) server
COMFYUI_BASE_URL=http://localhost:8188
```

Removed: `GROQ_API_KEY`, `HF_TOKEN`.

### 5. `requirements.txt`

App dependencies only: `fastapi`, `uvicorn[standard]`, `httpx`, `Pillow`,
`pydantic`, `python-dotenv`. Remove `openai-whisper` (was unused) and any
HF-specific bits.

### 6. Operational setup (docs + helper files)

- `docker-compose.yml` (or a documented `docker run`) to start Speaches on host
  port 8001 with the `whisper-large-v3` model, including a volume for model
  cache so it persists across restarts.
- README section: how to install/launch ComfyUI natively, where to place the
  FLUX.1-schnell fp8 model file (downloaded once, manually), and how to start
  all three services in order.
- README WebSocket protocol section stays the same; only the "setup" and
  "architecture" sections change.

## Data Flow (unchanged from the client's perspective)

```
text_input / binary-audio
  -> progress(stt)        [audio only]
  -> transcription        [audio only]
  -> progress(generating)
  -> result { image (PNG data-uri), prompt_used, raw_mono (1-bit b64), height }
  -> error  { stage, message }  on any failure
```

## Error Handling

Fully offline — **no cloud fallback**. If a local service is unreachable, the
app sends a clear WebSocket error naming the service to start. The app itself
still starts even if the services are down (services are checked at request
time, not required for boot).

## Testing Strategy

- Unit: `to_raw_mono` behavior is unchanged — add/keep a test asserting 384-wide,
  48-bytes-per-row, MSB-first, black=1 packing on a known input image.
- Unit: `stt.transcribe` and `image_gen.generate_*` against a mocked httpx
  transport (Speaches response shape; ComfyUI `/prompt` -> `/history` -> `/view`
  sequence), including the service-down error path.
- Manual/integration: run all three services, send text and audio via the
  browser client, confirm a 1-bit bitmap result with no network egress.
```
