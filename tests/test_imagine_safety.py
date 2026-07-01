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


@pytest.mark.asyncio
async def test_moderation_fails_open_without_key(monkeypatch):
    from app import moderation, config
    monkeypatch.setattr(config, "GROQ_API_KEY", None)
    safe, reason = await moderation.is_prompt_safe("anything at all")
    assert safe is True


@pytest.mark.asyncio
async def test_generation_blocked_when_moderation_flags_unsafe(monkeypatch):
    async def unsafe(subject, client=None):
        return False, "content not allowed for children"
    async def fake_hf(prompt, width=None, height=None):
        raise AssertionError("FLUX must not run when moderation blocks the subject")
    monkeypatch.setattr(image_gen.moderation, "is_prompt_safe", unsafe)
    monkeypatch.setattr(image_gen, "generate_with_huggingface", fake_hf)
    with pytest.raises(ValueError) as exc:
        await image_gen.generate_imagine_jpeg("some cleverly worded unsafe request")
    assert "safety_block" in str(exc.value)
