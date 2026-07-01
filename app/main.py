import base64
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.models import (
    ProgressMessage,
    TranscriptionMessage,
    ResultMessage,
    ErrorMessage,
    TextInput,
    PrintConfirm,
    PrintReject,
    client_message_adapter,
)
from app.image_gen import generate_line_art
from app.stt import transcribe

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HF_TOKEN = os.environ.get("HF_TOKEN")

WIDTH = 384
BYTES_PER_ROW = WIDTH // 8  # 48 bytes per row for a 384px-wide 1-bpp bitmap


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("GROQ_API_KEY"):
        logger.warning("GROQ_API_KEY not set. Audio transcription will fail.")
    logger.info("Server ready. Using Groq Whisper API for STT.")
    yield


app = FastAPI(title="Line Art Generator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


async def _safe_send(ws: WebSocket, payload: str, label: str) -> bool:
    """Send a text frame, swallowing the races where the socket is already gone."""
    try:
        await ws.send_text(payload)
        return True
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected before sending %s message", label)
        return False
    except RuntimeError as e:
        if 'Cannot call "send" once a close message has been sent' in str(e):
            logger.info("WebSocket already closing before sending %s message", label)
            return False
        raise


async def send_json(ws: WebSocket, msg) -> bool:
    return await _safe_send(ws, msg.model_dump_json(), msg.type)


def build_result_payload(raw_mono: str, height: int) -> str:
    """Build the device-facing `result` JSON: {type, raw_mono, width, height}.

    Validates the raw bitmap size and logs payload details so a tail of the
    device-side wire format is visible in the server logs.
    """
    decoded_size = len(base64.b64decode(raw_mono))
    expected_size = height * BYTES_PER_ROW
    if decoded_size != expected_size:
        raise ValueError(
            f"raw_mono decoded size {decoded_size} does not match expected "
            f"{expected_size} (height={height} * {BYTES_PER_ROW} bytes/row)"
        )

    payload = ResultMessage(raw_mono=raw_mono, height=height).model_dump_json()
    expected_suffix = f'","width":{WIDTH},"height":{height}}}'

    logger.info("AI Printer result JSON payload length: %d", len(payload))
    logger.info("AI Printer result JSON last 200 chars: %s", payload[-200:])
    logger.info(
        "AI Printer result JSON ends with expected suffix: %s",
        payload.endswith(expected_suffix),
    )
    return payload


async def generate_and_send(ws: WebSocket, subject: str):
    """Generate line art for an already-confirmed subject and send the result.

    Always terminates the device with exactly one `result` or `error` frame.
    """
    await send_json(ws, ProgressMessage(stage="generating", message=f"Generating line art for '{subject}'..."))

    try:
        _image_uri, _prompt_used, raw_mono, height = await generate_line_art(subject, HF_TOKEN)
        payload = build_result_payload(raw_mono, height)
        logger.info("Image generated: %dx%d, raw mono %d bytes", WIDTH, height, height * BYTES_PER_ROW)
        await _safe_send(ws, payload, "result")
    except Exception as e:
        logger.exception("Image generation failed")
        await send_json(ws, ErrorMessage(stage="image_gen", message=str(e)))


async def handle_text_input(ws: WebSocket, subject: str):
    """Eager text path (browser / other clients): generate immediately."""
    subject = subject.strip()
    if not subject:
        await send_json(ws, ErrorMessage(stage="input", message="Empty text input."))
        return
    logger.info("Text input received: '%s'", subject)
    await generate_and_send(ws, subject)


async def handle_audio_input(ws: WebSocket, audio_bytes: bytes) -> str | None:
    """Transcribe audio and PAUSE. Returns the transcription as the pending prompt
    (to be confirmed before generation), or None if transcription failed."""
    MAX_AUDIO_SIZE = 10 * 1024 * 1024  # ~10MB
    if len(audio_bytes) > MAX_AUDIO_SIZE:
        await send_json(ws, ErrorMessage(stage="input", message="Audio too large. Keep recordings under 10 seconds."))
        return None

    logger.info("Audio received: %d bytes (%.1f KB)", len(audio_bytes), len(audio_bytes) / 1024)
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
    # Do NOT generate yet — wait for print_confirm.
    return text


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    logger.info("WebSocket connected")

    pending_prompt: str | None = None  # one unconfirmed prompt in flight per connection

    try:
        while True:
            message = await ws.receive()

            if message["type"] != "websocket.receive":
                continue

            # ---- Binary frame = WAV audio: transcribe, then pause ----
            if "bytes" in message:
                # A new audio upload voids any earlier unconfirmed prompt.
                pending_prompt = await handle_audio_input(ws, message["bytes"])
                continue

            # ---- Text frame = a control message ----
            if "text" in message:
                try:
                    msg = client_message_adapter.validate_json(message["text"])
                except ValidationError as e:
                    await send_json(ws, ErrorMessage(stage="input", message=f"Invalid message: {e}"))
                    continue

                if isinstance(msg, PrintConfirm):
                    if not pending_prompt:
                        await send_json(ws, ErrorMessage(stage="input", message="No prompt to confirm."))
                        continue
                    prompt, pending_prompt = pending_prompt, None  # consume it
                    logger.info("print_confirm for '%s'", prompt)
                    await generate_and_send(ws, prompt)

                elif isinstance(msg, PrintReject):
                    logger.info("print_reject; discarding pending prompt '%s'", pending_prompt)
                    pending_prompt = None  # abort silently; send nothing

                elif isinstance(msg, TextInput):
                    await handle_text_input(ws, msg.text)

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except RuntimeError as e:
        if 'Cannot call "receive" once a disconnect message has been received' in str(e):
            logger.info("WebSocket receive loop ended after disconnect")
            return
        raise


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8010, reload=True)
