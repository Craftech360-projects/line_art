"""Cheeko device WebSocket session: hello handshake, Opus audio buffering,
and the line_art_* print message flow. See aiprinter-server-contract.md.
"""
import json
import logging
import uuid

from starlette.websockets import WebSocketDisconnect

from app import device_messages as dm
from app import opus_decode
from app import stt
from app import image_gen

logger = logging.getLogger(__name__)


async def handle_device_session(
    ws,
    first_message: dict,
    *,
    transcribe=stt.transcribe,
    generate_line_art=image_gen.generate_line_art,
    decode=opus_decode.decode_opus_to_wav,
) -> None:
    """Drive one device session. `first_message` is the parsed device hello."""
    session_id = uuid.uuid4().hex
    await ws.send_json(dm.hello_reply(session_id))
    logger.info("Device session %s started", session_id)

    listening = False
    opus_frames: list[bytes] = []

    try:
        while True:
            message = await ws.receive()
            mtype = message.get("type")
            if mtype == "websocket.disconnect":
                break
            if mtype != "websocket.receive":
                continue

            if "text" in message and message["text"] is not None:
                try:
                    data = json.loads(message["text"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if data.get("type") == "listen":
                    state = data.get("state")
                    if state == "start":
                        listening = True
                        opus_frames = []
                    elif state == "stop":
                        if not listening:
                            continue
                        listening = False
                        await _run_line_art(
                            ws, session_id, opus_frames, transcribe, generate_line_art, decode,
                        )
                        opus_frames = []
                # other text types (mcp, hello repeats, etc.) are ignored
            elif "bytes" in message and message["bytes"] is not None:
                if listening:
                    opus_frames.append(message["bytes"])
    except WebSocketDisconnect:
        pass
    finally:
        # Best-effort flush: audio buffered but never stopped.
        if opus_frames and listening:
            try:
                await _run_line_art(
                    ws, session_id, opus_frames, transcribe, generate_line_art, decode,
                )
            except Exception:
                logger.exception("flush failed for session %s", session_id)
    logger.info("Device session %s ended", session_id)


async def _run_line_art(ws, session_id, opus_frames, transcribe, generate_line_art, decode):
    """Decode -> transcribe -> generate -> emit the line_art_* sequence."""
    # 1. Decode + transcribe.
    try:
        wav = decode(opus_frames)
        text = (await transcribe(wav)).strip()
    except Exception as e:
        logger.exception("STT failed")
        await ws.send_json(dm.line_art_error(f"Transcription failed: {e}", stage="stt", session_id=session_id))
        return

    if not text:
        await ws.send_json(dm.line_art_error(
            "Could not transcribe any speech from audio.", stage="stt", session_id=session_id))
        return

    await ws.send_json(dm.line_art_transcription(text, session_id=session_id))
    await ws.send_json(dm.line_art_progress(
        f"Generating line art for '{text}'...", stage="image_gen", session_id=session_id))

    # 2. Generate.
    try:
        _data_uri, _prompt, raw_mono, height = await generate_line_art(text)
    except Exception as e:
        logger.exception("Image generation failed")
        await ws.send_json(dm.line_art_error(str(e), stage="image_gen", session_id=session_id))
        return

    await ws.send_json(dm.line_art(raw_mono, 384, height, session_id=session_id))
