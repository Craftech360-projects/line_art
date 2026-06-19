"""Decode raw Opus packets (as sent by the Cheeko device) to PCM16 WAV bytes.

The device sends BARE Opus packets over binary WebSocket frames — no Ogg
container, no header. PyAV's libopus decoder consumes these packets directly.
"""
import io
import logging
import wave

import av
import numpy as np

logger = logging.getLogger(__name__)


def _frame_to_int16(frame) -> "np.ndarray":
    """Flatten a decoded AudioFrame to int16 PCM, scaling float formats."""
    arr = frame.to_ndarray().reshape(-1)
    if np.issubdtype(arr.dtype, np.floating):
        # float samples are in [-1.0, 1.0]; scale to int16 range.
        return (np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16)
    return arr.astype(np.int16)


def decode_opus_to_wav(frames: list[bytes], sample_rate: int = 16000) -> bytes:
    """Decode raw Opus packets to a mono PCM16 WAV (bytes)."""
    decoder = av.CodecContext.create("libopus", "r")
    decoder.sample_rate = sample_rate
    decoder.format = "s16"
    decoder.layout = "mono"

    # The decoder is configured for s16 above; PyAV 17 on this platform emits
    # int16 frames (verified). If a future PyAV build decoded libopus to float
    # planar instead, frames would be fltp and the astype(int16) below would
    # clip near-silent — guard against that by checking the frame dtype.
    chunks = []
    for raw in frames:
        packet = av.packet.Packet(raw)
        for frame in decoder.decode(packet):
            chunks.append(_frame_to_int16(frame))
    for frame in decoder.decode(None):  # flush decoder
        chunks.append(_frame_to_int16(frame))

    if not chunks:
        raise ValueError("no audio decoded from opus frames")

    pcm = np.concatenate(chunks).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _encode_pcm_to_opus(pcm, sample_rate: int = 16000, frame_samples: int = 960) -> list[bytes]:
    """Test helper: encode PCM16 mono ndarray to a list of raw Opus packets."""
    encoder = av.CodecContext.create("libopus", "w")
    encoder.sample_rate = sample_rate
    encoder.format = "s16"
    encoder.layout = "mono"

    packets = []
    for i in range(0, len(pcm) - frame_samples, frame_samples):
        chunk = pcm[i:i + frame_samples]
        frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = sample_rate
        frame.pts = i
        for pkt in encoder.encode(frame):
            packets.append(bytes(pkt))
    for pkt in encoder.encode(None):  # flush
        packets.append(bytes(pkt))
    return packets
