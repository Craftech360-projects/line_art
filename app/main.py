import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from app.models import ProgressMessage, TranscriptionMessage, ResultMessage, ErrorMessage, TextInput
from app.image_gen import generate_line_art
from app.stt import transcribe
from app import config
from app.device_protocol import handle_device_session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set SAVE_INPUT_AUDIO=1 to dump each browser-path upload (the WAV the AiPrinter
# device sends) under debug_audio/ — handy for diagnosing bad transcriptions by
# playing back exactly what STT received. Mirrors SAVE_DEVICE_AUDIO on the device
# path. The browser frame is already a WAV blob, so it's saved as-is.
SAVE_INPUT_AUDIO = os.environ.get("SAVE_INPUT_AUDIO", "").lower() in ("1", "true", "yes")
_INPUT_AUDIO_DIR = Path("debug_audio")


def _save_input_wav(audio_bytes: bytes) -> None:
    if not SAVE_INPUT_AUDIO:
        return
    try:
        _INPUT_AUDIO_DIR.mkdir(exist_ok=True)
        path = _INPUT_AUDIO_DIR / f"{int(time.time())}_input.wav"
        path.write_bytes(audio_bytes)
        logger.info("Saved incoming input audio -> %s (%d bytes)", path, len(audio_bytes))
    except Exception:
        logger.exception("Failed to save input audio")

@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.SENTRY_DSN:
        import sentry_sdk
        sentry_sdk.init(dsn=config.SENTRY_DSN, environment=config.SENTRY_ENV,
                        traces_sample_rate=0.0)
        logger.info("Sentry error reporting enabled (env=%s)", config.SENTRY_ENV)
    if config.MANAGER_API_BASE_URL:
        stt_desc = (f"manager-api({config.MANAGER_API_BASE_URL}, ttl={config.STT_PROVIDER_TTL_S:.0f}s) "
                    f"last-resort={config.STT_LAST_RESORT_PROVIDER}")
    else:
        stt_desc = f"last-resort only ({config.STT_LAST_RESORT_PROVIDER})"
    if config.IMAGE_BACKEND == "comfyui":
        img_desc = f"ComfyUI @ {config.COMFYUI_BASE_URL}"
    else:
        img_desc = "HuggingFace FLUX" + ("" if config.HF_API_TOKEN else " [HF_API_TOKEN missing!]")
    if config.MODERATION_BACKEND == "off":
        mod_desc = "off (keyword-only)"
    else:
        mod_desc = "manager-api-selected (env Groq last resort)" if config.GROQ_API_KEY \
            else "manager-api-selected (no env last resort)"
    logger.info("Server ready. STT=%s | ImageGen=%s | Moderation=%s", stt_desc, img_desc, mod_desc)
    yield


app = FastAPI(title="Line Art Generator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}


async def send_json(ws: WebSocket, msg):
    await ws.send_text(msg.model_dump_json())


async def handle_text_input(ws: WebSocket, subject: str):
    """Process text subject -> line art image."""
    subject = subject.strip()
    if not subject:
        await send_json(ws, ErrorMessage(stage="input", message="Empty text input."))
        return

    logger.info("Text input received: '%s'", subject)
    await send_json(ws, ProgressMessage(stage="generating", message=f"Generating line art for '{subject}'..."))

    try:
        image_data_uri, prompt_used, raw_mono, height = await generate_line_art(subject)
        raw_size = len(raw_mono) * 3 // 4  # approx decoded size
        logger.info("Image generated: 384x%d, raw mono ~%d bytes", height, raw_size)
        await send_json(ws, ResultMessage(image=image_data_uri, prompt_used=prompt_used, raw_mono=raw_mono, height=height))
    except Exception as e:
        logger.exception("Image generation failed")
        await send_json(ws, ErrorMessage(stage="image_gen", message=str(e)))


async def handle_audio_input(ws: WebSocket, audio_bytes: bytes) -> str | None:
    """Process audio -> transcription. Sends `transcription` and RETURNS the text
    (the pending prompt) — generation is gated behind a later print_confirm.
    Returns None if the audio was too large or STT was empty/failed (error sent)."""
    MAX_AUDIO_SIZE = 10 * 1024 * 1024  # ~10MB
    if len(audio_bytes) > MAX_AUDIO_SIZE:
        await send_json(ws, ErrorMessage(stage="input", message="Audio too large. Keep recordings under 10 seconds."))
        return None

    logger.info("Audio received: %d bytes (%.1f KB)", len(audio_bytes), len(audio_bytes) / 1024)
    if SAVE_INPUT_AUDIO:
        _save_input_wav(audio_bytes)
    await send_json(ws, ProgressMessage(stage="stt", message="Transcribing audio..."))

    try:
        text = await transcribe(audio_bytes)
    except Exception as e:
        logger.exception("Transcription failed")
        await send_json(ws, ErrorMessage(stage="stt", message=f"Transcription failed: {e}"))
        return None

    if not text:
        logger.warning("STT returned empty transcription")
        await send_json(ws, ErrorMessage(stage="stt", message="Could not transcribe any speech from audio."))
        return None

    logger.info("Transcription result: '%s'", text)
    await send_json(ws, TranscriptionMessage(text=text))
    return text


async def _process_browser_message(ws: WebSocket, message: dict, pending_text):
    """Handle one browser-protocol frame. `pending_text` is the transcription
    awaiting a decision (or None). Returns the new pending_text."""
    if "bytes" in message and message["bytes"] is not None:
        # New audio voids any prior un-confirmed transcription.
        return await handle_audio_input(ws, message["bytes"])

    if "text" in message and message["text"] is not None:
        try:
            data = json.loads(message["text"])
        except json.JSONDecodeError as e:
            await send_json(ws, ErrorMessage(stage="input", message=f"Invalid message: {e}"))
            return pending_text

        mtype = data.get("type") if isinstance(data, dict) else None
        if mtype == "print_confirm":
            if pending_text:
                await handle_text_input(ws, pending_text)
            return None  # consumed (or no-op if nothing pending)
        if mtype == "print_reject":
            return None  # abort; send nothing

        # Otherwise treat it as a typed text_input (generates immediately).
        try:
            parsed = TextInput(**data)
        except (TypeError, ValueError) as e:
            await send_json(ws, ErrorMessage(stage="input", message=f"Invalid message: {e}"))
            return pending_text
        await handle_text_input(ws, parsed.text)
        return None  # typed text also clears any pending audio prompt

    return pending_text


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    logger.info("WebSocket connected")

    try:
        # Peek the first message to choose protocol: a `hello` => device.
        first = await ws.receive()
        if first.get("type") == "websocket.disconnect":
            return
        if first.get("type") == "websocket.receive" and first.get("text"):
            try:
                parsed = json.loads(first["text"])
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict) and parsed.get("type") == "hello":
                await handle_device_session(ws, parsed)
                return
        # Not a device hello: process this first message, then continue the
        # existing browser loop.
        pending_text = await _process_browser_message(ws, first, None)
        while True:
            message = await ws.receive()
            if message.get("type") != "websocket.receive":
                if message.get("type") == "websocket.disconnect":
                    break
                continue
            pending_text = await _process_browser_message(ws, message, pending_text)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
