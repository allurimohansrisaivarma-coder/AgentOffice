"""Settings, read once from the environment and the private .env file.

Values copied unchanged from .env.example (e.g. "your-api-key-here") count as
unset, so `cp .env.example .env` never sends a placeholder to a real service.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

PROVIDERS = ("meterless", "openai_compatible")


def _get(name: str, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    if value.startswith("your-") or value.startswith("https://api.your-provider"):
        return ""
    return value


def _get_bool(name: str, default: bool = False) -> bool:
    value = _get(name)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    value = _get(name)
    try:
        return int(value) if value else default
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {value!r}") from None


@dataclass(frozen=True)
class Settings:
    # Which generator llm.py builds. Meterless Nightshift is the primary provider.
    llm_provider: str
    meterless_base_url: str
    meterless_runtime_token: str
    meterless_model: str
    meterless_app: str
    # Any other OpenAI-compatible endpoint (secondary provider).
    model_base_url: str
    model_api_key: str
    model_name: str
    zetaris_rest_url: str
    zetaris_org_id: str
    zetaris_api_key: str
    sample_mode: bool
    log_level: str
    log_dir: Path
    port: int
    http_timeout_s: float = 30.0
    model_timeout_s: float = 90.0
    max_retries: int = 2

    def secret_values(self) -> list[str]:
        """Every configured secret, so the tracer can mask it wherever it appears."""
        candidates = (self.meterless_runtime_token, self.model_api_key, self.zetaris_api_key)
        return [s for s in candidates if s]

    def missing_for_default_mode(self) -> list[str]:
        """Names of env vars that default (non-sample) mode needs but are unset."""
        missing = []
        if self.llm_provider == "meterless":
            if not self.meterless_base_url:
                missing.append("METERLESS_BASE_URL")
            if not self.meterless_runtime_token:
                missing.append("METERLESS_RUNTIME_TOKEN")
        else:
            for name, value in (("MODEL_BASE_URL", self.model_base_url),
                                ("MODEL_API_KEY", self.model_api_key),
                                ("MODEL_NAME", self.model_name)):
                if not value:
                    missing.append(name)
        if not self.zetaris_rest_url:
            missing.append("ZETARIS_REST_URL")
        if not self.zetaris_api_key:
            missing.append("ZETARIS_API_KEY")
        return missing


def load_settings(env_file: Optional[Path] = ROOT / ".env") -> Settings:
    if env_file is not None and env_file.exists():
        # Real environment variables win over the file (Docker --env-file, CI).
        load_dotenv(env_file, override=False)

    provider = _get("LLM_PROVIDER", "meterless").lower()
    if provider not in PROVIDERS:
        raise ValueError(f"LLM_PROVIDER must be one of {PROVIDERS}, got {provider!r}")

    log_dir = Path(_get("LOG_DIR") or ROOT / "logs")
    return Settings(
        llm_provider=provider,
        meterless_base_url=_get("METERLESS_BASE_URL").rstrip("/"),
        meterless_runtime_token=_get("METERLESS_RUNTIME_TOKEN"),
        meterless_model=_get("METERLESS_MODEL", "meterless/auto"),
        meterless_app=_get("METERLESS_APP", "agentoffice"),
        model_base_url=_get("MODEL_BASE_URL").rstrip("/"),
        model_api_key=_get("MODEL_API_KEY"),
        model_name=_get("MODEL_NAME"),
        zetaris_rest_url=_get("ZETARIS_REST_URL").rstrip("/"),
        zetaris_org_id=_get("ZETARIS_ORG_ID", "1"),
        zetaris_api_key=_get("ZETARIS_API_KEY"),
        sample_mode=_get_bool("SAMPLE_MODE"),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
        log_dir=log_dir,
        port=_get_int("PORT", 8000),
    )
