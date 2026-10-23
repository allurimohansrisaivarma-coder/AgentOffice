"""The single model seam. Agents receive a Generator and never build a client.

Swapping providers is one env var (LLM_PROVIDER). Meterless Nightshift is the
default. In sample mode there is no generator at all: agents must use their
own data-driven fallback and log `model: offline-fallback`, never a fake reply.
"""

from __future__ import annotations

from typing import Optional, Protocol

from app.adapters.meterless import ChatResult, meterless_client, openai_compatible_client
from app.config import Settings


class Generator(Protocol):
    @property
    def name(self) -> str: ...

    def chat(self, messages: list[dict], *, temperature: float = 0.2, **params) -> ChatResult: ...


def build_generator(settings: Settings) -> Optional[Generator]:
    if settings.sample_mode:
        return None
    if settings.llm_provider == "meterless":
        return meterless_client(settings)
    return openai_compatible_client(settings)
