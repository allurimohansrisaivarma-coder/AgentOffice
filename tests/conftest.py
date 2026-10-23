from dataclasses import replace

import pytest

from app import config
from app.adapters import http as http_adapter
from app.config import Settings


@pytest.fixture
def make_settings(tmp_path):
    def _make(**overrides) -> Settings:
        base = Settings(
            llm_provider="meterless",
            meterless_base_url="http://nightshift.test/v1",
            meterless_runtime_token="mtok-test-1234567890",
            meterless_model="meterless/test-route",
            meterless_app="agentoffice-tests",
            model_base_url="",
            model_api_key="",
            model_name="",
            zetaris_rest_url="http://zetaris.test/api/v1.0",
            zetaris_org_id="1",
            zetaris_api_key="ztok-test-1234567890",
            sample_mode=False,
            log_level="INFO",
            log_dir=tmp_path / "logs",
            port=8000,
        )
        return replace(base, **overrides)
    return _make


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(http_adapter, "BACKOFF_BASE_S", 0)


@pytest.fixture
def isolated_env(monkeypatch):
    """Clear every variable load_settings reads, so the developer's .env can't leak in."""
    for name in ("LLM_PROVIDER", "METERLESS_BASE_URL", "METERLESS_RUNTIME_TOKEN",
                 "METERLESS_MODEL", "METERLESS_APP", "MODEL_BASE_URL", "MODEL_API_KEY",
                 "MODEL_NAME", "ZETARIS_REST_URL", "ZETARIS_ORG_ID", "ZETARIS_API_KEY",
                 "SAMPLE_MODE", "LOG_LEVEL", "LOG_DIR", "PORT"):
        monkeypatch.delenv(name, raising=False)
    return config
