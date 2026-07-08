# AI Printer — Line Art Generator

A **fully offline** WebSocket server that turns voice or text into a 1-bit
line-art bitmap, ready to print on the Cheeko "AI Printer" device (an ESP32
thermal/e-ink printer) or to render in a browser.

Speech-to-text and image generation both run **locally** — no cloud, no API keys.

```
voice / text  ──►  Whisper (STT)  ──►  FLUX.1-schnell (image)  ──►  1-bit bitmap  ──►  device prints it
```

## What it does

- Accepts **two protocols on one `/ws` endpoint**, auto-detected by the first message:
  - **Device protocol** (Cheeko firmware): `hello` handshake + raw Opus audio + `line_art_*` print messages.
  - **Browser protocol** (the bundled test page): `text_input` JSON or raw WAV bytes.
- Transcribes speech with a **local Speaches** (Whisper) container.
- Generates line art with a **local ComfyUI** running **FLUX.1-schnell fp8** on the GPU.
- Converts the result to the device's **1-bit, 384-px-wide** packed bitmap and streams it back.

---

## Architecture

```
                     FastAPI app  (:8090)
   device / browser   ┌───────────────────────────┐
   ──── ws /ws ──────►│  app/main.py  → /ws router │
                      │   ├─ device_protocol.py    │──► Speaches (Docker :8001)  STT  (faster-whisper-large-v3)
                      │   └─ browser handlers      │──► ComfyUI  (Docker :8188)  IMG  (FLUX.1-schnell fp8, GPU)
                      └───────────────────────────┘──► Pillow: resize 384 + 1-bit threshold
```

| Service | Where | Port | Role |
|---------|-------|------|------|
| FastAPI app | this project | **8090** | WebSocket server (`/ws`) |
| Speaches | Docker | 8001 → 8000 | speech-to-text (Whisper) |
| ComfyUI | Docker (GPU) | 8188 | image generation (FLUX.1-schnell) |

> **Port note:** the app runs on **8090**, not 8000 — on the dev machine port 8000
> is taken by another process. Change it with `--port` if you like.

The app boots even if Speaches/ComfyUI are down; requests that need them return a
clear error until they are up.

---

## Setup

**Requirements:** Docker Desktop with NVIDIA GPU support (the RTX-class GPU is used
for both Whisper and FLUX), Python 3.11, ~20 GB free disk for the FLUX checkpoint.

### 1. Speaches (speech-to-text)

```bash
docker compose up -d speaches
# pull the Whisper model once (or via the UI at http://localhost:8001):
curl -X POST "http://localhost:8001/v1/models/Systran/faster-whisper-large-v3"
curl http://localhost:8001/v1/models           # verify it's listed
```

The model cache is a Docker volume (`hf-hub-cache`), so it survives restarts.

### 2. ComfyUI (image generation)

ComfyUI runs as a **local Docker image** built from `comfyui.Dockerfile` (official
ComfyUI on a CUDA PyTorch base, run as root so Windows bind mounts work).

```bash
docker compose build comfyui     # first time only
docker compose up -d comfyui
```

**Download the FLUX checkpoint** (~17 GB) and place it where the container mounts
its models — `comfyui-data/basedir/models/checkpoints/` on the host:

```
flux1-schnell-fp8.safetensors
```

Get it from the `Comfy-Org/flux1-schnell` repo (the all-in-one fp8 file that bundles
CLIP + T5 + VAE, so `CheckpointLoaderSimple` can load it). The filename must be
exactly `flux1-schnell-fp8.safetensors`.

Verify ComfyUI sees it:

```bash
curl http://localhost:8188/                      # UI responds
curl http://localhost:8188/object_info/CheckpointLoaderSimple   # lists the checkpoint
```

> **First generation is slow** (~minutes) while the 17 GB model loads into VRAM;
> every generation after that is ~5 s. The `restart: unless-stopped` policy keeps
> the model warm.

### 3. App

```bash
pip install -r requirements.txt
copy .env.example .env            # defaults already point at the local services
uvicorn app.main:app --host 0.0.0.0 --port 8090 --reload
```

Open the browser test client at:

- `http://localhost:8090/static/device.html` — device-protocol demo (handshake + prints the bitmap on a canvas)
- `http://localhost:8090/static/index.html` — original text/voice client

### Startup order

Start Speaches + ComfyUI first, then the app. The app starts regardless and returns
clear errors for requests that arrive before the services are ready.

---

## Configuration (`.env`)

```
SPEACHES_BASE_URL=http://localhost:8001
SPEACHES_MODEL=Systran/faster-whisper-large-v3
COMFYUI_BASE_URL=http://localhost:8188

# Optional debug / tuning:
SAVE_DEVICE_AUDIO=1      # dump each utterance's decoded WAV to debug_audio/
MONO_THRESHOLD=190       # 1-bit cutoff (higher = bolder lines; default 190)
```

### Multi-provider STT

The primary STT provider is resolved from cheeko-backend's manager-api `GET /providers/active`
(cached for `STT_PROVIDER_TTL_S` seconds). On hard failures (HTTP 5xx, timeouts, connection
errors), the app falls back to `STT_LAST_RESORT_PROVIDER` (fixed in `.env`). See
[`docs/adr/0002-stt-provider-selection-via-manager-api.md`](docs/adr/0002-stt-provider-selection-via-manager-api.md)
for details.

### Multi-provider moderation

The child-safety moderation provider is resolved the same way as STT: manager-api's
`GET /providers/active` now returns a `moderation` block (backed by the
`moderation_providers` table — providers: `groq`, `openai`, `openrouter`,
`openai_moderation`). The env-configured Groq judge (`GROQ_API_KEY` +
`GROQ_LLM_MODEL`) is the fixed last resort, and the whole layer still fails open
to the keyword filter if every provider is down. Switch the active provider with
`PUT /livekit/providers/active/moderation {"provider": "openai", "model": "gpt-4o-mini", "api_key": "..."}`.

The server also saves a copy of every generated image to `generated_images/`
(both the full-colour FLUX PNG and the 1-bit mono PNG the device prints).

### Multi-provider image generation

The image backend is resolved the same way: manager-api's `GET /providers/active`
returns an `image` block (table `image_providers` — providers: `hf`, `runware`,
`fal`; variant rows like `runware_schnell` route by base name). The env HF token
(`HF_API_TOKEN`) is the fixed last resort, and `IMAGE_BACKEND=comfyui` still
forces the local ComfyUI path. Generation failures fall through the chain; the
imagine path still serves `IMAGINE_FALLBACK_IMAGE` if everything fails. Switch with
`PUT /livekit/providers/active/image {"provider":"runware","model":"runware:400@4","api_key":"..."}`
(or the non-clobbering `PUT /livekit/providers/image/:id/active`).

---

## Connecting the device

The Cheeko device connects to:

```
ws://<SERVER_LAN_IP>:8090/ws
```

Find your IP with `ipconfig` (use the Wi-Fi `IPv4 Address`, e.g. `192.168.0.186`).
The IP is DHCP-assigned and can change — if the device stops connecting, re-check it.
Device and server must be on the same network.

---

## WebSocket protocols

### A) Device protocol (Cheeko firmware)

The authoritative wire contract is in [`aiprinter-server-contract.md`](aiprinter-server-contract.md).

```
device                                  server
  │── hello ──────────────────────────►│
  │◄─ hello {transport:websocket,       │   (must reply < 10 s)
  │          session_id, audio_params}  │
  │── listen {state:start} ────────────►│
  │── <raw Opus binary frames> ────────►│   (16 kHz mono, 60 ms, no Ogg)
  │── listen {state:stop} ─────────────►│
  │◄─ line_art_transcription {text} ────│
  │◄─ line_art_progress {message,stage} │
  │◄─ line_art {raw_mono,width,height} ─│   ◄── device prints this
```

- Errors come back as `line_art_error {message, stage}`.
- Every server→device message echoes `session_id`.
- Audio is **raw Opus packets** (no Ogg container). The server decodes them with
  **opuslib** (a direct libopus binding) — **not** PyAV, whose FFmpeg wrapper forces
  48 kHz output and corrupts the 16 kHz stream.

### B) Browser protocol (test client)

**Send:** `{"type":"text_input","text":"a cat"}` (JSON) or raw WAV bytes (binary).

**Receive:**
```json
{"type":"progress","stage":"generating","message":"Generating line art for 'a cat'..."}
{"type":"transcription","text":"a cat"}          // audio input only
{"type":"result","image":"data:image/png;base64,...","prompt_used":"...",
 "raw_mono":"<base64 1-bit bitmap>","width":384,"height":384}
{"type":"error","stage":"image_gen","message":"..."}
```

---

## Raw bitmap format (`raw_mono`)

Base64-encoded packed 1-bit bitmap. After decoding:

| Property    | Value                              |
|-------------|------------------------------------|
| Width       | 384 px (always)                    |
| Height      | variable (aspect preserved)        |
| Depth       | 1-bit — black = 1, white = 0       |
| Bit order   | MSB first (leftmost pixel = bit 7) |
| Row order   | top-down                           |
| Bytes/row   | 48 (384 / 8)                       |
| Header/pad/compression | none                    |

Total size = `height * 48` bytes. Reading pixel (x, y):

```c
uint8_t byte = raw[y * 48 + x / 8];
bool is_black = (byte >> (7 - (x % 8))) & 1;
```

The 1-bit conversion uses a **brightness threshold** (`MONO_THRESHOLD`), not
dithering — so thin line art stays solid instead of being broken into speckle.

---

## Test clients

| Client | Use |
|--------|-----|
| `ai_printer_client.py` | CLI device client — mic (`--wav file` to send a file); encodes real raw Opus, prints the bitmap to a PNG. `--url`, `--out`. |
| `ai_printer_gui.py` | Tkinter GUI — Start/Stop recording, shows the printed bitmap. Run with the Python that has `sounddevice`. |
| `static/device.html` | Browser device demo — handshake + canvas render (text-driven; browsers can't emit bare Opus). |
| `device_e2e_test.py` | Device-protocol integration harness (hello → Opus → line_art). |
| `e2e_test.py` | Browser-protocol harness (text → result). |
| `client.py` | Reference MQTT/UDP client for the *other* Cheeko transport variant (not this server). |

Quick check (server + services running):

```bash
python ai_printer_client.py --wav some_speech.wav --url ws://localhost:8090/ws
```

---

## Project structure

```
line_art/
├── app/
│   ├── main.py            # FastAPI app, /ws protocol router
│   ├── config.py          # env config (Speaches + ComfyUI URLs)
│   ├── stt.py             # Speaches (Whisper) speech-to-text
│   ├── opus_decode.py     # raw Opus → WAV via opuslib (device audio)
│   ├── device_protocol.py # Cheeko device session (hello, Opus, line_art_*)
│   ├── device_messages.py # builders for server→device messages
│   ├── image_gen.py       # FLUX call + resize + 1-bit threshold conversion
│   ├── comfy_client.py    # ComfyUI submit/poll/fetch
│   ├── comfy_workflow.py  # ComfyUI FLUX.1-schnell prompt graph
│   └── models.py          # Pydantic schemas (browser protocol)
├── static/
│   ├── device.html        # device-protocol browser demo
│   └── index.html         # text/voice browser client
├── tests/                 # pytest suite
├── ai_printer_client.py   # CLI device test client
├── ai_printer_gui.py      # GUI device test client
├── device_e2e_test.py     # device-protocol harness
├── e2e_test.py            # browser-protocol harness
├── comfyui.Dockerfile     # local ComfyUI image
├── docker-compose.yml     # Speaches + ComfyUI services
├── aiprinter-server-contract.md  # device wire protocol (source of truth)
├── .env.example
└── requirements.txt
```

---

## Tests

```bash
python -m pytest -q
```

Covers the config, Speaches client, Opus decode (real libopus round-trip), the
ComfyUI workflow/client, the device session + message builders, the `/ws` protocol
routing, and the 1-bit bitmap packing.
