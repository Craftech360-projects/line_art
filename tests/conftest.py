import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path so `import app...` works under pytest.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _no_live_providers(monkeypatch):
    """app.main calls load_dotenv() at import, so full-suite runs inherit real
    keys from .env and provider chains would make LIVE API calls. Neutralize
    the env-driven providers; tests that need them set their own values."""
    from app import config
    monkeypatch.setattr(config, "HF_API_TOKEN", None)
    monkeypatch.setattr(config, "MANAGER_API_BASE_URL", "")
