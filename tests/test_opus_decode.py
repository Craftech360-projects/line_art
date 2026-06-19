import io
import wave

import numpy as np

from app.opus_decode import decode_opus_to_wav, _encode_pcm_to_opus


def _make_tone(sr=16000, seconds=1.0, hz=440.0):
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    return (np.sin(2 * np.pi * hz * t) * 12000).astype(np.int16)


def test_round_trip_opus_to_wav():
    sr = 16000
    pcm = _make_tone(sr)
    frames = _encode_pcm_to_opus(pcm, sample_rate=sr)
    assert len(frames) > 10  # ~49 frames for 1s at 60ms
    assert all(isinstance(f, bytes) and len(f) > 0 for f in frames)

    wav = decode_opus_to_wav(frames, sample_rate=sr)
    assert wav[:4] == b"RIFF"

    # WAV is parseable, mono, 16-bit, right rate, and roughly 1s of audio.
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == sr
        assert w.getnframes() > sr // 2  # at least half a second decoded


def test_empty_frames_raises():
    import pytest
    with pytest.raises(ValueError, match="no audio decoded"):
        decode_opus_to_wav([], sample_rate=16000)
