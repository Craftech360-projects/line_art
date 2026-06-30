import importlib


def _reload_config():
    import app.config
    return importlib.reload(app.config)


def test_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    monkeypatch.delenv("HF_API_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_API_TOKEN", raising=False)
    monkeypatch.delenv("HF_MODEL_URL", raising=False)
    cfg = _reload_config()
    assert cfg.GROQ_API_KEY is None
    assert cfg.GROQ_MODEL == "whisper-large-v3"
    assert cfg.HF_API_TOKEN is None
    assert "FLUX.1-schnell" in cfg.HF_MODEL_URL


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gk-123")
    monkeypatch.setenv("HF_API_TOKEN", "hf-456")
    cfg = _reload_config()
    assert cfg.GROQ_API_KEY == "gk-123"
    assert cfg.HF_API_TOKEN == "hf-456"
