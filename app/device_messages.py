"""Builders for server->device JSON messages (Cheeko firmware protocol).

Each returns a plain dict. `session_id` is included only when provided so the
device echoes it back. Optional fields are omitted when None.
"""


def _with_session(msg: dict, session_id: str | None) -> dict:
    if session_id is not None:
        # Place session_id right after type for readability.
        return {"type": msg["type"], "session_id": session_id,
                **{k: v for k, v in msg.items() if k != "type"}}
    return msg


def hello_reply(session_id: str, sample_rate: int = 16000, frame_duration: int = 60) -> dict:
    return {
        "type": "hello",
        "transport": "websocket",
        "session_id": session_id,
        "audio_params": {"sample_rate": sample_rate, "frame_duration": frame_duration},
    }


def line_art_transcription(text: str, session_id: str | None = None) -> dict:
    return _with_session({"type": "line_art_transcription", "text": text}, session_id)


def line_art_progress(message: str, stage: str | None = None, session_id: str | None = None) -> dict:
    msg = {"type": "line_art_progress", "message": message}
    if stage is not None:
        msg["stage"] = stage
    return _with_session(msg, session_id)


def line_art_error(message: str, stage: str | None = None, session_id: str | None = None) -> dict:
    msg = {"type": "line_art_error", "message": message}
    if stage is not None:
        msg["stage"] = stage
    return _with_session(msg, session_id)


def line_art(raw_mono: str, width: int, height: int, session_id: str | None = None) -> dict:
    return _with_session(
        {"type": "line_art", "raw_mono": raw_mono, "width": width, "height": height},
        session_id,
    )


def image(image_b64: str, width: int, height: int, caption: str | None = None,
          mime: str = "image/jpeg", session_id: str | None = None) -> dict:
    """AI Imagine result sent to the gateway: base64 JPEG bytes + dimensions.

    The gateway uploads the bytes to S3 and builds the device-facing `image{url}`.
    """
    msg = {"type": "image", "image": image_b64, "mime": mime,
           "width": width, "height": height}
    if caption is not None:
        msg["caption"] = caption
    return _with_session(msg, session_id)
