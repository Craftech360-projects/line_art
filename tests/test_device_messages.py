from app import device_messages as dm


def test_hello_reply_shape():
    msg = dm.hello_reply("sess-1", sample_rate=16000, frame_duration=60)
    assert msg["type"] == "hello"
    assert msg["transport"] == "websocket"  # firmware REQUIRES this exact value
    assert msg["session_id"] == "sess-1"
    assert msg["audio_params"] == {"sample_rate": 16000, "frame_duration": 60}


def test_transcription_includes_session_id():
    msg = dm.line_art_transcription("a cat", session_id="s2")
    assert msg == {"type": "line_art_transcription", "session_id": "s2", "text": "a cat"}


def test_progress_optional_stage_and_session():
    # No stage, no session -> neither key present.
    assert dm.line_art_progress("working") == {"type": "line_art_progress", "message": "working"}
    # With stage + session.
    msg = dm.line_art_progress("gen", stage="image_gen", session_id="s3")
    assert msg == {
        "type": "line_art_progress", "session_id": "s3",
        "message": "gen", "stage": "image_gen",
    }


def test_error_shape():
    msg = dm.line_art_error("boom", stage="stt", session_id="s4")
    assert msg == {
        "type": "line_art_error", "session_id": "s4", "message": "boom", "stage": "stt",
    }


def test_line_art_shape():
    msg = dm.line_art("AAAA", 384, 240, session_id="s5")
    assert msg == {
        "type": "line_art", "session_id": "s5",
        "raw_mono": "AAAA", "width": 384, "height": 240,
    }
