"""OpenAI-compatible chat client, configured as Meterless Nightshift by default.

Nightshift contract (AGENTS.md 5.2): Bearer runtime token, `meterless-app` and
`meterless-scope: user` headers, GET /models for discovery, POST
/chat/completions for chat. The same client talks to any other
OpenAI-compatible endpoint when the Meterless headers are left out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.adapters.http import AdapterError, json_body, request


@dataclass
class ChatResult:
    content: str
    model: str            # the model the server says actually answered
    usage: dict = field(default_factory=dict)
    response_id: Optional[str] = None


class ChatClient:
    def __init__(self, base_url: str, api_key: str, model: str, *, service: str,
                 timeout_s: float = 90.0, max_retries: int = 2,
                 extra_headers: Optional[dict] = None,
                 transport: Optional[httpx.BaseTransport] = None):
        if not base_url or not api_key or not model:
            raise AdapterError(service, "not_configured",
                               "base URL, API key/token and model must all be set",
                               recoverable=False)
        self.service = service
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        headers = {"Authorization": f"Bearer {api_key}", **(extra_headers or {})}
        self._client = httpx.Client(timeout=timeout_s, transport=transport, headers=headers)

    @property
    def name(self) -> str:
        return f"{self.service}/{self.model}"

    def list_models(self) -> list[str]:
        response = request(self._client, "GET", f"{self.base_url}/models",
                           service=self.service, max_retries=self.max_retries)
        body = json_body(response, self.service)
        return [m.get("id", "") for m in body.get("data", []) if isinstance(m, dict)]

    def chat(self, messages: list[dict], *, temperature: float = 0.2,
             model: Optional[str] = None, **params) -> ChatResult:
        payload = {"model": model or self.model, "messages": messages,
                   "temperature": temperature, **params}
        response = request(self._client, "POST", f"{self.base_url}/chat/completions",
                           service=self.service, max_retries=self.max_retries, json=payload)
        body = json_body(response, self.service)
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AdapterError(self.service, "invalid_response",
                               f"no choices[0].message.content in: {str(body)[:200]}",
                               recoverable=False, status_code=response.status_code) from None
        return ChatResult(content=content or "", model=body.get("model") or payload["model"],
                          usage=body.get("usage") or {}, response_id=body.get("id"))

    def close(self) -> None:
        self._client.close()


def meterless_client(settings, transport: Optional[httpx.BaseTransport] = None) -> ChatClient:
    return ChatClient(
        settings.meterless_base_url, settings.meterless_runtime_token, settings.meterless_model,
        service="meterless", timeout_s=settings.model_timeout_s,
        max_retries=settings.max_retries, transport=transport,
        extra_headers={"meterless-app": settings.meterless_app, "meterless-scope": "user"},
    )


def openai_compatible_client(settings, transport: Optional[httpx.BaseTransport] = None) -> ChatClient:
    return ChatClient(
        settings.model_base_url, settings.model_api_key, settings.model_name,
        service="model", timeout_s=settings.model_timeout_s,
        max_retries=settings.max_retries, transport=transport,
    )
