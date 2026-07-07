# tests/test_stt_fallback.py
import pytest
from app import stt, config
from app.stt_providers import ProviderConfig, STTHardFailure


@pytest.mark.asyncio
async def test_primary_success_no_fallback(monkeypatch):
    primary = ProviderConfig("deepgram", "nova-2", "", "dk")
    monkeypatch.setattr(stt.manager_client, "get_active_stt",
                        lambda client=None: _async(primary))
    calls = []
    async def fake_tw(cfg, audio, client=None):
        calls.append(cfg.provider)
        return "hello"
    monkeypatch.setattr(stt.stt_providers, "transcribe_with", fake_tw)
    assert await stt.transcribe(b"wav") == "hello"
    assert calls == ["deepgram"]  # last-resort never tried


@pytest.mark.asyncio
async def test_hard_failure_falls_to_last_resort(monkeypatch):
    primary = ProviderConfig("deepgram", "nova-2", "", "dk")
    monkeypatch.setattr(stt.manager_client, "get_active_stt",
                        lambda client=None: _async(primary))
    monkeypatch.setattr(config, "STT_LAST_RESORT_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    monkeypatch.setattr(config, "GROQ_MODEL", "whisper-large-v3")
    calls = []
    async def fake_tw(cfg, audio, client=None):
        calls.append(cfg.provider)
        if cfg.provider == "deepgram":
            raise STTHardFailure("429")
        return "recovered"
    monkeypatch.setattr(stt.stt_providers, "transcribe_with", fake_tw)
    assert await stt.transcribe(b"wav") == "recovered"
    assert calls == ["deepgram", "groq"]


@pytest.mark.asyncio
async def test_empty_text_is_returned_not_retried(monkeypatch):
    primary = ProviderConfig("groq", "m", "", "gk")
    monkeypatch.setattr(stt.manager_client, "get_active_stt",
                        lambda client=None: _async(primary))
    calls = []
    async def fake_tw(cfg, audio, client=None):
        calls.append(cfg.provider)
        return "   "
    monkeypatch.setattr(stt.stt_providers, "transcribe_with", fake_tw)
    assert await stt.transcribe(b"wav") == ""
    assert calls == ["groq"]  # empty is a terminal no-speech, not a fallback trigger


@pytest.mark.asyncio
async def test_no_active_provider_uses_last_resort(monkeypatch):
    monkeypatch.setattr(stt.manager_client, "get_active_stt",
                        lambda client=None: _async(None))
    monkeypatch.setattr(config, "STT_LAST_RESORT_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    async def fake_tw(cfg, audio, client=None):
        return f"via-{cfg.provider}"
    monkeypatch.setattr(stt.stt_providers, "transcribe_with", fake_tw)
    assert await stt.transcribe(b"wav") == "via-groq"


async def _async(value):
    return value
