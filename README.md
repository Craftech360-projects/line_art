# Line Art Generator API

WebSocket API that accepts audio or text input, generates a line art image using AI, and returns a raw 1-bit monochrome bitmap ready for embedded displays.

## How It Works

```
Device/Browser                          Server (this project)
     |                                       |
     |── ws://SERVER_IP:8000/ws ────────────>|
     |                                       |
     |── audio (binary frame) ─────────────>|── Speaches (Docker :8001) for STT
     |   OR text (JSON frame)                |── ComfyUI (native Windows :8188) for image gen
     |                                       |── Pillow (resize + 1-bit convert)
     |                                       |
     |<──── JSON: transcription + image ─────|
     |                                       |
```

## Fully Offline Setup

All AI processing runs locally — no cloud API keys required.

### Architecture

```
FastAPI app (:8000)
    ├── Speaches (Docker)   :8001  — speech-to-text  (Whisper large-v3)
    └── ComfyUI (native)    :8188  — image generation (FLUX.1-schnell fp8)
```

The app boots even if Speaches or ComfyUI are not yet running. Requests that need those services return a clear error message until they are up.

---

### 1. Speaches (Speech-to-Text)

Speaches runs in Docker and exposes an OpenAI-compatible `/v1/audio/transcriptions` endpoint.

**Requirements:** Docker Desktop with NVIDIA GPU support (NVIDIA Container Toolkit).

**Start the container:**

```bash
docker compose up -d speaches
```

**Pull the Whisper model once** (the container must be running):

```bash
curl -X POST "http://localhost:8001/v1/models/Systran/faster-whisper-large-v3"
```

Alternatively, open the Speaches UI at `http://localhost:8001` and download the model from there.

**Verify:**

```bash
curl http://localhost:8001/v1/models
```

You should see `Systran/faster-whisper-large-v3` listed.

---

### 2. ComfyUI (Image Generation) — Native Windows

ComfyUI runs natively on Windows (not in Docker) and exposes a REST/WebSocket API.

**Install ComfyUI:**

Download the portable build from https://github.com/comfyanonymous/ComfyUI/releases and follow its README to set it up.

**Download the model checkpoint:**

Download `flux1-schnell-fp8.safetensors` (search for it on the model hub of your choice, e.g. the `city96/FLUX.1-schnell-gguf` repo or the official `black-forest-labs/FLUX.1-schnell` repo) and place it at:

```
ComfyUI/models/checkpoints/flux1-schnell-fp8.safetensors
```

The filename must be exactly `flux1-schnell-fp8.safetensors`.

**Start ComfyUI:**

```bash
python main.py --listen 0.0.0.0 --port 8188
```

Or, if using the portable build with an NVIDIA GPU:

```bash
run_nvidia_gpu.bat
```

**Verify:**

Open `http://localhost:8188` in a browser — you should see the ComfyUI interface.

---

### 3. App

**Install dependencies:**

```bash
pip install -r requirements.txt
```

**Configure environment:**

```bash
copy .env.example .env
```

The defaults in `.env.example` already point at the local services:

```
SPEACHES_BASE_URL=http://localhost:8001
SPEACHES_MODEL=Systran/faster-whisper-large-v3
COMFYUI_BASE_URL=http://localhost:8188
```

No API keys are needed.

**Start the server:**

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Open the test client:**

`http://127.0.0.1:8000/static/index.html`

---

### 4. Startup Order

Start Speaches and ComfyUI **before** the app for a smooth first request, but it is not required — the app will start regardless and return clear errors for any request that arrives before the services are ready.

Recommended order:

1. `docker compose up -d speaches`
2. Start ComfyUI (`python main.py --listen 0.0.0.0 --port 8188` or `run_nvidia_gpu.bat`)
3. `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`

---

## Device Connection

Devices connect to a single WebSocket endpoint:

```
ws://<SERVER_IP>:8000/ws
```

Example: `ws://192.168.1.168:8000/ws`

No authentication required. The device and server must be on the same network.

### Find Your Server IP

```bash
ipconfig
```

Look for `IPv4 Address` under `Wireless LAN adapter Wi-Fi` (e.g., `192.168.1.168`).

---

## WebSocket Protocol

### Sending to Server

**Option A: Audio (binary frame)**

Send raw WAV audio as a binary WebSocket frame.

| Parameter       | Value          |
|-----------------|----------------|
| Format          | WAV            |
| Sample rate     | 16000 Hz       |
| Channels        | 1 (mono)       |
| Bit depth       | 16-bit PCM     |
| Max size        | 10 MB          |

**Option B: Text (JSON text frame)**

```json
{"type": "text_input", "text": "cat"}
```

### Receiving from Server

All responses are JSON text frames. Parse the `"type"` field:

**Progress (informational):**
```json
{"type": "progress", "stage": "stt", "message": "Transcribing audio..."}
{"type": "progress", "stage": "generating", "message": "Generating line art for 'cat'..."}
```

**Transcription (audio input only):**
```json
{"type": "transcription", "text": "cat"}
```

**Result:**
```json
{
  "type": "result",
  "image": "data:image/png;base64,...",
  "prompt_used": "simple black and white line art drawing of cat...",
  "raw_mono": "<base64 encoded raw bitmap>",
  "width": 384,
  "height": 384
}
```

**Error:**
```json
{"type": "error", "stage": "stt", "message": "Transcription failed: ..."}
```

## Raw Bitmap Format (`raw_mono` field)

The `raw_mono` field is a base64-encoded raw 1-bit monochrome bitmap. After base64 decoding:

| Property     | Value                                |
|--------------|--------------------------------------|
| Width        | 384 pixels (always)                  |
| Height       | Variable (preserved aspect ratio)    |
| Color depth  | 1-bit (black = 1, white = 0)         |
| Bit order    | MSB first (leftmost pixel = bit 7)   |
| Row order    | Top-down                             |
| Bytes/row    | 48 (384 / 8)                         |
| Padding      | None                                 |
| Header       | None                                 |
| Compression  | None                                 |

Total size = `height * 48` bytes.

### Example: Reading pixel (x, y)

```c
uint8_t byte = raw_data[y * 48 + x / 8];
bool is_black = (byte >> (7 - (x % 8))) & 1;
```

## Message Flow

```
DEVICE                                SERVER
  |                                      |
  |──── Connect ws://IP:8000/ws ───────>|
  |                                      |
  |──── Binary: WAV audio ────────────>|
  |                                      |
  |<──── {"type":"progress",             |
  |       "stage":"stt"} ──────────────|
  |                                      |
  |<──── {"type":"transcription",        |
  |       "text":"cat"} ──────────────|
  |                                      |
  |<──── {"type":"progress",             |
  |       "stage":"generating"} ───────|
  |                                      |
  |<──── {"type":"result",               |
  |       "raw_mono":"...",              |
  |       "width":384,                   |
  |       "height":384} ──────────────|
  |                                      |
  |  [base64 decode raw_mono]            |
  |  [render 1-bit bitmap on display]    |
  |                                      |
```

## Project Structure

```
line_art/
├── app/
│   ├── __init__.py
│   ├── main.py             # FastAPI app, WebSocket endpoint
│   ├── config.py           # Env var config (Speaches + ComfyUI URLs)
│   ├── stt.py              # Speaches Whisper API for speech-to-text
│   ├── image_gen.py        # ComfyUI FLUX.1-schnell + 1-bit conversion
│   ├── comfy_workflow.py   # Builds the ComfyUI prompt graph
│   └── models.py           # Pydantic message schemas
├── static/
│   └── index.html          # Browser test client
├── docker-compose.yml      # Speaches service definition
├── .env.example            # Environment variable template
├── requirements.txt
└── README.md
```

## Server-Side Services

These are called by the server only. The device does NOT need any API keys.

| Service         | Provider                                   | Purpose                     |
|-----------------|--------------------------------------------|-----------------------------|
| Speech-to-Text  | Speaches (Docker :8001, Whisper large-v3)  | Transcribes audio to text   |
| Image Generation| ComfyUI (native :8188, FLUX.1-schnell fp8) | Generates line art from text|
| Image Conversion| Local (Pillow)                             | Resizes + converts to 1-bit |
