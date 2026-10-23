import pytest


def test_placeholders_count_as_unset(isolated_env, monkeypatch):
    monkeypatch.setenv("ZETARIS_API_KEY", "your-zetaris-api-key")
    monkeypatch.setenv("METERLESS_RUNTIME_TOKEN", "your-nightshift-runtime-token")
    s = isolated_env.load_settings(env_file=None)
    assert s.zetaris_api_key == ""
    assert "ZETARIS_API_KEY" in s.missing_for_default_mode()
    assert "METERLESS_RUNTIME_TOKEN" in s.missing_for_default_mode()


def test_defaults(isolated_env):
    s = isolated_env.load_settings(env_file=None)
    assert s.llm_provider == "meterless"
    assert s.meterless_model == "meterless/auto"
    assert s.sample_mode is False
    assert s.port == 8000


def test_unknown_provider_rejected(isolated_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        isolated_env.load_settings(env_file=None)
