import json
import logging
from contextlib import asynccontextmanager

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Server ready (offline). STT=Speaches@%s model=%s | ImageGen=ComfyUI@%s",
        config.SPEACHES_BASE_URL,
        config.SPEACHES_MODEL,
        config.COMFYUI_BASE_URL,
    )
    yield


app = FastAPI(title="Line Art Generator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


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


async def handle_audio_input(ws: WebSocket, audio_bytes: bytes):
    """Process audio -> transcription -> line art image."""
    MAX_AUDIO_SIZE = 10 * 1024 * 1024  # ~10MB
    if len(audio_bytes) > MAX_AUDIO_SIZE:
        await send_json(ws, ErrorMessage(stage="input", message="Audio too large. Keep recordings under 10 seconds."))
        return

    logger.info("Audio received: %d bytes (%.1f KB)", len(audio_bytes), len(audio_bytes) / 1024)
    await send_json(ws, ProgressMessage(stage="stt", message="Transcribing audio..."))

    try:
        text = await transcribe(audio_bytes)
    except Exception as e:
        logger.exception("Transcription failed")
        await send_json(ws, ErrorMessage(stage="stt", message=f"Transcription failed: {e}"))
        return

    if not text:
        logger.warning("STT returned empty transcription")
        await send_json(ws, ErrorMessage(stage="stt", message="Could not transcribe any speech from audio."))
        return

    logger.info("Transcription result: '%s'", text)
    await send_json(ws, TranscriptionMessage(text=text))
    await handle_text_input(ws, text)


async def _process_browser_message(ws: WebSocket, message: dict):
    """Handle one message in the existing browser protocol."""
    if "text" in message and message["text"] is not None:
        try:
            data = json.loads(message["text"])
            parsed = TextInput(**data)
            await handle_text_input(ws, parsed.text)
        except (json.JSONDecodeError, ValueError) as e:
            await send_json(ws, ErrorMessage(stage="input", message=f"Invalid message: {e}"))
    elif "bytes" in message and message["bytes"] is not None:
        await handle_audio_input(ws, message["bytes"])


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
        await _process_browser_message(ws, first)
        while True:
            message = await ws.receive()
            if message.get("type") != "websocket.receive":
                if message.get("type") == "websocket.disconnect":
                    break
                continue
            await _process_browser_message(ws, message)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
