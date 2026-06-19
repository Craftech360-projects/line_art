import io

from PIL import Image

from app import image_gen


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_raw_mono_is_384_wide_48_bytes_per_row():
    # 768x512 white image -> resized to 384 wide, height 256
    img = Image.new("RGB", (768, 512), (255, 255, 255))
    _, raw = image_gen.to_raw_mono(_png_bytes(img))
    assert len(raw) == 256 * 48  # height 256, 48 bytes/row


def test_all_black_sets_all_bits_one():
    img = Image.new("RGB", (384, 8), (0, 0, 0))
    _, raw = image_gen.to_raw_mono(_png_bytes(img))
    assert len(raw) == 8 * 48
    assert all(b == 0xFF for b in raw)  # black=1, every bit set


def test_all_white_sets_all_bits_zero():
    img = Image.new("RGB", (384, 8), (255, 255, 255))
    _, raw = image_gen.to_raw_mono(_png_bytes(img))
    assert all(b == 0x00 for b in raw)  # white=0


def test_msb_first_left_pixel_is_high_bit():
    # Left half black, right half white on a single 384x1 row.
    img = Image.new("1", (384, 1), 1)  # all white in mode "1"
    for x in range(192):
        img.putpixel((x, 0), 0)  # left half black
    _, raw = image_gen.to_raw_mono(_png_bytes(img))
    assert raw[0] == 0xFF      # first 8 px black -> 11111111
    assert raw[24] == 0x00     # byte 24 covers px 192..199 -> white
