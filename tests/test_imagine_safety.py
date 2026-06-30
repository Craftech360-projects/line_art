"""AI Imagine prompt-cleaning + child-safety guard tests."""
import pytest
from app import image_gen


def test_command_prefix_is_stripped_from_subject():
    prompt = image_gen.build_imagine_prompt("can you draw a beautiful cat")
    assert "a beautiful cat" in prompt
    # The conversational/command words must not survive into the FLUX prompt.
    assert "can you" not in prompt.lower()
    assert "draw" not in prompt.lower()


def test_prompt_tells_flux_no_text():
    assert "no text" in image_gen.build_imagine_prompt("a dog").lower()


def test_unsafe_subject_raises_safety_block():
    with pytest.raises(ValueError) as exc:
        image_gen.build_imagine_prompt("a scary zombie covered in blood")
    assert "safety_block" in str(exc.value)


def test_safe_subject_is_allowed():
    prompt = image_gen.build_imagine_prompt("a happy puppy playing in a park")
    assert "a happy puppy playing in a park" in prompt
