"""Meterless Runtime (Nightshift) as the primary model provider, plus a generic
OpenAI-compatible client that shares the same code.

Provider contract (meterless.ai/engines.html, POWER "Add Meterless as a provider"):
- id `meterless`, name "Meterless Runtime", default base URL http://127.0.0.1:8088/v1;
  pasted URLs are normalized to <host>/v1 and never end in /v1/v1
- bearer runtime token on every call; the placeholder `rt-local` is rejected
- `meterless-app` header on every request, `meterless-scope: user` on chat
- model ids are routing modes (meterless/<disposition>, meterless/<class>,
  meterless/<class>.<disposition>) read from GET /v1/models; unknown
  meterless/* ids are kept
- status comes from GET /v1/models, not /healthz: 401/403 -> unauthorized,
  200 with meterless ids -> online, anything else -> offline
- auto-detect tries ports 8088, 8090, 8787, 11434 on 127.0.0.1, localhost, [::1]
- connect links http://<host>:<port>/connect#k=<key> carry base URL and key
- 180 s timeout, since a request may wait for a model to load
Chat is streamed for Nightshift, so the timeout applies to silence between
chunks; MAX_STREAM_S bounds a whole reply.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, urlsplit

import httpx

from app.adapters import http as _http
from app.adapters.http import AdapterError, json_body, request, should_retry

PROVIDER_ID = "meterless"
PROVIDER_NAME = "Meterless Runtime"
DEFAULT_BASE_URL = "http://127.0.0.1:8088/v1"
MODEL_LOAD_TIMEOUT_S = 180.0
MAX_STREAM_S = 900.0
PLACEHOLDER_TOKENS = frozenset({"rt-local"})
DETECT_PORTS = (8088, 8090, 8787, 11434)
DETECT_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_ROUTE = re.compile(r"^meterless/[a-z0-9_\-]+(\.[a-z0-9_\-]+)?$")


@dataclass
class ChatResult:
    content: str
    model: str            # the model the server says actually answered
    usage: dict = field(default_factory=dict)
    response_id: Optional[str] = None
    finish_reason: Optional[str] = None   # "length" = cut off at max_tokens


def normalize_base_url(url: str) -> str:
    """'127.0.0.1:8088', 'http://h:8088/v1/v1/', 'http://h:8088/v1/chat/completions' -> 'http://h:8088/v1'."""
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if not parts.netloc:
        raise ValueError(f"not a URL: {url!r}")
    return f"{parts.scheme}://{parts.netloc}/v1"


def parse_connect_link(link: str) -> tuple[str, str]:
    """http://<host>:<port>/connect#k=<key> -> (base URL, key). Never log the key."""
    parts = urlsplit((link or "").strip())
    key = (parse_qs(parts.fragment).get("k") or [""])[0]
    if not parts.netloc or not parts.path.rstrip("/").endswith("/connect") or not key:
        raise ValueError("expected a connect link like http://<host>:<port>/connect#k=<key>")
    return f"{parts.scheme or 'http'}://{parts.netloc}/v1", key


def is_route(model_id: str) -> bool:
    """A recognised meterless/<class>[.<disposition>] routing mode."""
    return bool(_ROUTE.match(model_id or ""))


def check_token(token: str) -> None:
    if not token:
        raise AdapterError(PROVIDER_ID, "not_configured", "METERLESS_RUNTIME_TOKEN is not set",
                           recoverable=False)
    if token.strip() in PLACEHOLDER_TOKENS:
        raise AdapterError(PROVIDER_ID, "not_configured",
                           "the runtime token is the placeholder 'rt-local'; copy the real one "
                           "from Nightshift Console", recoverable=False)


class ChatClient:
    def __init__(self, base_url: str, api_key: str, model: str, *, service: str,
                 timeout_s: float = 90.0, max_retries: int = 2,
                 extra_headers: Optional[dict] = None, chat_headers: Optional[dict] = None,
                 transport: Optional[httpx.BaseTransport] = None, stream: bool = False):
        if not base_url or not api_key or not model:
            raise AdapterError(service, "not_configured",
                               "base URL, API key/token and model must all be set",
                               recoverable=False)
        self.service = service
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.stream = stream
        self._chat_headers = dict(chat_headers or {})
        headers = {"Authorization": f"Bearer {api_key}", **(extra_headers or {})}
        self._client = httpx.Client(timeout=timeout_s, transport=transport, headers=headers)

    @property
    def name(self) -> str:
        # "meterless/deep", not "meterless/meterless/deep", when the model id is already a route
        return self.model if self.model.startswith(f"{self.service}/") else f"{self.service}/{self.model}"

    def list_models(self) -> list[str]:
        response = request(self._client, "GET", f"{self.base_url}/models",
                           service=self.service, max_retries=self.max_retries)
        body = json_body(response, self.service)
        return [m.get("id", "") for m in body.get("data", []) if isinstance(m, dict)]

    def chat(self, messages: list[dict], *, temperature: float = 0.2,
             model: Optional[str] = None, **params) -> ChatResult:
        payload = {"model": model or self.model, "messages": messages,
                   "temperature": temperature, **params}
        if self.stream:
            return self._chat_stream(payload)
        response = request(self._client, "POST", f"{self.base_url}/chat/completions",
                           service=self.service, max_retries=self.max_retries,
                           json=payload, headers=self._chat_headers)
        return self._result(json_body(response, self.service), payload, response.status_code)

    def _chat_stream(self, payload: dict) -> ChatResult:
        """Streamed chat: the read timeout applies between chunks, so a long answer from a slow reasoning
        model is not cut off, while MAX_STREAM_S still bounds the whole reply. A connection that fails
        before any data arrives is retried like any request; a stream that dies midway is not."""
        url = f"{self.base_url}/chat/completions"
        body = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        started, attempts = time.monotonic(), 0
        while True:
            attempts += 1
            parts: list[str] = []
            meta: dict = {"model": payload["model"], "usage": {}, "finish_reason": None, "id": None}
            got_data = False
            try:
                with self._client.stream("POST", url, json=body, headers=self._chat_headers) as response:
                    status = response.status_code
                    if not response.is_success:
                        response.read()
                        if should_retry(status) and attempts <= self.max_retries:
                            time.sleep(_http.BACKOFF_BASE_S * 2 ** (attempts - 1))
                            continue
                        raise AdapterError(self.service, "http_error" if status not in (401, 403) else "auth",
                                           f"HTTP {status}: {response.text[:300]}", recoverable=should_retry(status),
                                           status_code=status, attempts=attempts)
                    if response.headers.get("content-type", "").startswith("application/json"):
                        response.read()   # the server ignored stream=true
                        return self._result(json_body(response, self.service), payload, status)
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        got_data = True
                        if chunk.get("error"):
                            raise AdapterError(self.service, "http_error", f"stream error: {str(chunk['error'])[:300]}",
                                               recoverable=True, status_code=status, attempts=attempts)
                        meta["model"] = chunk.get("model") or meta["model"]
                        meta["id"] = chunk.get("id") or meta["id"]
                        meta["usage"] = chunk.get("usage") or meta["usage"]
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            if isinstance(delta.get("content"), str):
                                parts.append(delta["content"])
                            meta["finish_reason"] = choice.get("finish_reason") or meta["finish_reason"]
                        if time.monotonic() - started > MAX_STREAM_S:
                            raise AdapterError(self.service, "timeout", f"no complete reply after {MAX_STREAM_S:g}s",
                                               recoverable=True, status_code=status, attempts=attempts)
            except httpx.HTTPError as e:
                if not got_data and attempts <= self.max_retries:
                    time.sleep(_http.BACKOFF_BASE_S * 2 ** (attempts - 1))
                    continue
                raise AdapterError(self.service, "timeout" if isinstance(e, httpx.TimeoutException) else "unreachable",
                                   f"{type(e).__name__}: {e}", recoverable=True, attempts=attempts) from None
            if not got_data:
                raise AdapterError(self.service, "invalid_response", "the stream carried no data", recoverable=True,
                                   status_code=status, attempts=attempts)
            return ChatResult(content="".join(parts), model=meta["model"], usage=meta["usage"],
                              response_id=meta["id"], finish_reason=meta["finish_reason"])

    def _result(self, body: Any, payload: dict, status_code: int) -> ChatResult:
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AdapterError(self.service, "invalid_response",
                               f"no choices[0].message.content in: {str(body)[:200]}",
                               recoverable=False, status_code=status_code) from None
        if isinstance(content, list):   # some servers return content parts; plain text stays a string
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return ChatResult(content=content or "", model=body.get("model") or payload["model"],
                          usage=body.get("usage") or {}, response_id=body.get("id"),
                          finish_reason=body["choices"][0].get("finish_reason"))

    def close(self) -> None:
        self._client.close()


def probe(base_url: str, token: str = "", *, app_name: str = "agentoffice", timeout_s: float = 3.0,
          transport: Optional[httpx.BaseTransport] = None) -> dict:
    """Provider status from GET /v1/models: online, unauthorized or offline. No retries."""
    base_url = normalize_base_url(base_url)
    headers = {"meterless-app": app_name}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=timeout_s, transport=transport) as client:
            response = client.get(f"{base_url}/models", headers=headers)
    except httpx.HTTPError as e:
        return {"base_url": base_url, "status": "offline", "detail": f"{type(e).__name__}: {e}", "models": []}
    if response.status_code in (401, 403):
        return {"base_url": base_url, "status": "unauthorized",
                "detail": f"HTTP {response.status_code}: the runtime token was refused", "models": []}
    models: list[str] = []
    if response.status_code == 200:
        try:
            models = [m.get("id", "") for m in response.json().get("data", []) if isinstance(m, dict)]
        except (ValueError, AttributeError):
            models = []
    meterless_ids = [m for m in models if m.startswith("meterless/")]
    if response.status_code == 200 and meterless_ids:
        return {"base_url": base_url, "status": "online",
                "detail": f"{len(models)} models, {len(meterless_ids)} meterless routes", "models": models}
    return {"base_url": base_url, "status": "offline",
            "detail": f"HTTP {response.status_code}, {len(meterless_ids)} meterless routes", "models": models}


def auto_detect(token: str = "", *, app_name: str = "agentoffice", ports: Iterable[int] = DETECT_PORTS,
                hosts: Iterable[str] = DETECT_HOSTS, timeout_s: float = 1.0,
                transport: Optional[httpx.BaseTransport] = None) -> Optional[dict]:
    """First host:port whose /v1/models answers online or unauthorized, else None."""
    for port in ports:
        for host in hosts:
            result = probe(f"http://{host}:{port}", token, app_name=app_name,
                           timeout_s=timeout_s, transport=transport)
            if result["status"] in ("online", "unauthorized"):
                return result
    return None


def meterless_client(settings, transport: Optional[httpx.BaseTransport] = None) -> ChatClient:
    check_token(settings.meterless_runtime_token)
    return ChatClient(
        normalize_base_url(settings.meterless_base_url), settings.meterless_runtime_token,
        settings.meterless_model, service=PROVIDER_ID,
        timeout_s=max(settings.model_timeout_s, MODEL_LOAD_TIMEOUT_S),
        max_retries=settings.max_retries, transport=transport,
        extra_headers={"meterless-app": settings.meterless_app},
        chat_headers={"meterless-scope": "user"},
        stream=True,   # Nemotron writing a long module can take minutes; stream so only silence times out
    )


def openai_compatible_client(settings, transport: Optional[httpx.BaseTransport] = None) -> ChatClient:
    return ChatClient(
        settings.model_base_url, settings.model_api_key, settings.model_name,
        service="model", timeout_s=settings.model_timeout_s,
        max_retries=settings.max_retries, transport=transport,
    )
