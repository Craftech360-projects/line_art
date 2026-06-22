"""Decode raw Opus packets (as sent by the Cheeko device) to PCM16 WAV bytes.

The device sends BARE Opus packets over binary WebSocket frames — no Ogg
container, no header. We decode with opuslib (a direct libopus binding),
exactly like the reference firmware client (cheeko-backend/client.py) and the
mqtt-gateway (@discordjs/opus). NOT PyAV: FFmpeg's libopus wrapper ignores the
requested output rate and always emits 48 kHz, so bare 16 kHz packets came out
3x too long and pitch-crushed.
"""
import io
import logging
import wave

import numpy as np
import opuslib

logger = logging.getLogger(__name__)

CHANNELS = 1
# Max samples/channel a single packet can hold (120 ms @ 48 kHz) — the safe
# decode buffer opuslib wants regardless of the actual 60 ms frame size.
_MAX_FRAME_SAMPLES = 5760


def decode_opus_to_wav(frames: list[bytes], sample_rate: int = 16000) -> bytes:
    """Decode raw Opus packets to a mono PCM16 WAV (bytes)."""
    decoder = opuslib.Decoder(sample_rate, CHANNELS)
    chunks = [decoder.decode(raw, _MAX_FRAME_SAMPLES) for raw in frames]

    if not chunks:
        raise ValueError("no audio decoded from opus frames")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"".join(chunks))
    return buf.getvalue()


def _encode_pcm_to_opus(pcm, sample_rate: int = 16000, frame_samples: int = 960) -> list[bytes]:
    """Test helper: encode PCM16 mono ndarray to a list of raw Opus packets."""
    encoder = opuslib.Encoder(sample_rate, CHANNELS, opuslib.APPLICATION_VOIP)
    pcm = np.asarray(pcm, dtype=np.int16)
    return [
        encoder.encode(pcm[i:i + frame_samples].tobytes(), frame_samples)
        for i in range(0, len(pcm) - frame_samples + 1, frame_samples)
    ]
