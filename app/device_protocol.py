"""Cheeko device WebSocket session: hello handshake, Opus audio buffering,
and the line_art_* print message flow. See aiprinter-server-contract.md.
"""
import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path

from starlette.websockets import WebSocketDisconnect

from app import device_messages as dm
from app import opus_decode
from app import stt
from app import image_gen

logger = logging.getLogger(__name__)

# Set SAVE_DEVICE_AUDIO=1 to dump each utterance's decoded WAV under debug_audio/
# (handy for diagnosing bad transcriptions — play it back to hear what STT got).
SAVE_DEVICE_AUDIO = os.environ.get("SAVE_DEVICE_AUDIO", "").lower() in ("1", "true", "yes")
_AUDIO_DIR = Path("debug_audio")

# Whisper hallucinates phrases ("Thank you.", "you") on near-silent blips, and each one
# costs a full image generation. Frames are ~60ms opus packets, so 5 ≈ 300ms — shorter
# than any real utterance. ponytail: fixed floor; make smarter (RMS check) if it misfires.
MIN_UTTERANCE_FRAMES = int(os.environ.get("MIN_UTTERANCE_FRAMES", "5"))


def _save_debug_wav(session_id: str, wav_bytes: bytes) -> None:
    try:
        _AUDIO_DIR.mkdir(exist_ok=True)
        # time.time() is fine here (runtime side effect, not in a workflow script).
        path = _AUDIO_DIR / f"{int(time.time())}_{session_id[:8]}.wav"
        path.write_bytes(wav_bytes)
        logger.info("Saved incoming device audio -> %s (%d bytes)", path, len(wav_bytes))
    except Exception:
        logger.exception("Failed to save debug audio")


async def _transcribe_and_prompt(ws, session_id, opus_frames, transcribe, decode):
    """Decode + transcribe; send line_art_transcription. Returns the text to
    print (the pending prompt), or None if STT was empty/failed (error sent)."""
    if len(opus_frames) < MIN_UTTERANCE_FRAMES:
        logger.info("[session %s] utterance too short (%d frames < %d) — skipping STT",
                    session_id[:8], len(opus_frames), MIN_UTTERANCE_FRAMES)
        await ws.send_json(dm.line_art_error(
            "Could not transcribe any speech from audio.", stage="stt", session_id=session_id))
        return None
    try:
        wav = decode(opus_frames)
        if SAVE_DEVICE_AUDIO:
            _save_debug_wav(session_id, wav)
        text = (await transcribe(wav)).strip()
    except Exception as e:
        logger.exception("STT failed")
        await ws.send_json(dm.line_art_error(f"Transcription failed: {e}", stage="stt", session_id=session_id))
        return None

    if not text:
        await ws.send_json(dm.line_art_error(
            "Could not transcribe any speech from audio.", stage="stt", session_id=session_id))
        return None

    await ws.send_json(dm.line_art_transcription(text, session_id=session_id))
    return text


async def _generate_and_send(ws, session_id, text, generate_line_art):
    """Generate the bitmap for a confirmed prompt and send line_art (or error)."""
    await ws.send_json(dm.line_art_progress(
        f"Generating line art for '{text}'...", stage="image_gen", session_id=session_id))
    try:
        _data_uri, _prompt, raw_mono, height = await generate_line_art(text)
    except Exception as e:
        logger.exception("Image generation failed")
        await ws.send_json(dm.line_art_error(str(e), stage="image_gen", session_id=session_id))
        return
    await ws.send_json(dm.line_art(raw_mono, 384, height, session_id=session_id))


async def _generate_imagine_and_send(ws, session_id, text, generate_imagine):
    """Imagine mode: generate a color JPEG immediately (no print_confirm) and
    send it as an `image` message. The gateway uploads it and builds image{url}."""
    await ws.send_json(dm.line_art_progress(
        f"Imagining '{text}'...", stage="image_gen", session_id=session_id))
    logger.info("[imagine %s] generation started for %r", session_id[:8], text)
    t0 = time.time()
    try:
        jpeg, _prompt = await generate_imagine(text)
    except Exception as e:
        logger.exception("[imagine %s] generation FAILED after %.1fs", session_id[:8], time.time() - t0)
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(e)
        except Exception:
            pass
        await ws.send_json(dm.line_art_error(str(e), stage="image_gen", session_id=session_id))
        return
    image_b64 = base64.b64encode(jpeg).decode()
    await ws.send_json(dm.image(image_b64, 320, 240, caption=text, session_id=session_id))
    logger.info(
        "[imagine %s] image message SENT to gateway: jpeg=%d bytes b64=%d chars (%.1fs total). "
        "Upload to S3 happens gateway-side (manager-api) per ADR-0001 — check gateway logs next.",
        session_id[:8], len(jpeg), len(image_b64), time.time() - t0)


async def handle_device_session(
    ws,
    first_message: dict,
    *,
    transcribe=stt.transcribe,
    generate_line_art=image_gen.generate_line_art,
    generate_imagine=image_gen.generate_imagine_jpeg,
    decode=opus_decode.decode_opus_to_wav,
) -> None:
    """Drive one device session. `first_message` is the parsed device hello."""
    from app import config as _cfg
    if _cfg.WS_SHARED_SECRET and first_message.get("auth") != _cfg.WS_SHARED_SECRET:
        logger.warning("Rejected device hello: bad/missing auth")
        await ws.close(code=1008)
        return
    session_id = uuid.uuid4().hex
    imagine = first_message.get("feature") == "ai_imagine"
    await ws.send_json(dm.hello_reply(session_id))
    logger.info("Device session %s started (mode=%s)",
                session_id, "imagine" if imagine else "printer")

    listening = False
    disconnected = False
    opus_frames: list[bytes] = []
    pending_text = None  # transcription awaiting print_confirm / print_reject

    try:
        while True:
            message = await ws.receive()
            mtype = message.get("type")
            if mtype == "websocket.disconnect":
                disconnected = True
                break
            if mtype != "websocket.receive":
                continue

            if "text" in message and message["text"] is not None:
                try:
                    data = json.loads(message["text"])
                except (json.JSONDecodeError, TypeError):
                    continue
                mtype_in = data.get("type")
                if mtype_in == "listen":
                    state = data.get("state")
                    if state == "start":
                        listening = True
                        opus_frames = []
                        pending_text = None  # new audio voids any un-confirmed prompt
                        logger.info("[session %s] listen start — buffering audio", session_id[:8])
                    elif state == "stop":
                        if not listening:
                            continue
                        listening = False
                        logger.info("[session %s] listen stop — %d opus frames buffered, transcribing...",
                                    session_id[:8], len(opus_frames))
                        text = await _transcribe_and_prompt(
                            ws, session_id, opus_frames, transcribe, decode,
                        )
                        opus_frames = []
                        if imagine:
                            if text:
                                await _generate_imagine_and_send(
                                    ws, session_id, text, generate_imagine)
                            pending_text = None  # imagine never waits for confirm
                        else:
                            pending_text = text
                elif mtype_in == "print_confirm":
                    if pending_text:
                        text = pending_text
                        pending_text = None
                        await _generate_and_send(ws, session_id, text, generate_line_art)
                    # no pending -> ignore
                elif mtype_in == "print_reject":
                    pending_text = None  # abort; send nothing
                # other text types (mcp, hello repeats, etc.) are ignored
            elif "bytes" in message and message["bytes"] is not None:
                if listening:
                    opus_frames.append(message["bytes"])
    except WebSocketDisconnect:
        disconnected = True
    finally:
        # No auto-generation on session end: generation only happens on an
        # explicit print_confirm. Buffered audio that never got a listen-stop is
        # simply dropped. (pending_text, if any, was never confirmed -> void.)
        pass
    logger.info("Device session %s ended", session_id)
