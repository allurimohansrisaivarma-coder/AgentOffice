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


def test_localhost_points_at_the_host_inside_a_container(isolated_env, monkeypatch):
    monkeypatch.setattr(isolated_env, "in_container", lambda: True)
    monkeypatch.setenv("ZETARIS_REST_URL", "http://localhost:8888/api/v1.0")
    monkeypatch.setenv("METERLESS_BASE_URL", "127.0.0.1:8088")
    monkeypatch.setenv("MODEL_BASE_URL", "https://api.example.org/v1")
    s = isolated_env.load_settings(env_file=None)
    assert s.zetaris_rest_url == "http://host.docker.internal:8888/api/v1.0"
    assert s.meterless_base_url == "http://host.docker.internal:8088/v1"
    assert s.model_base_url == "https://api.example.org/v1"          # remote hosts untouched

    monkeypatch.setenv("AGENTOFFICE_NO_HOST_REWRITE", "true")
    assert isolated_env.load_settings(env_file=None).zetaris_rest_url == "http://localhost:8888/api/v1.0"


def test_no_rewrite_outside_a_container(isolated_env, monkeypatch):
    monkeypatch.setenv("ZETARIS_REST_URL", "http://localhost:8888/api/v1.0")
    assert isolated_env.load_settings(env_file=None).zetaris_rest_url == "http://localhost:8888/api/v1.0"


def test_unknown_provider_rejected(isolated_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        isolated_env.load_settings(env_file=None)
