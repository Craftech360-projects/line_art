from app import device_messages as dm


def test_image_message_shape():
    msg = dm.image("QUJD", 320, 240, caption="a cat", session_id="s1")
    assert msg["type"] == "image"
    assert msg["session_id"] == "s1"
    assert msg["image"] == "QUJD"
    assert msg["mime"] == "image/jpeg"
    assert msg["width"] == 320 and msg["height"] == 240
    assert msg["caption"] == "a cat"


def test_image_message_omits_caption_when_none():
    msg = dm.image("QUJD", 320, 240)
    assert "caption" not in msg
    assert "session_id" not in msg
