"""Shared HTTP plumbing: one place for timeouts, bounded retries and error mapping."""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

BACKOFF_BASE_S = 0.5


class AdapterError(Exception):
    """A failed call to an external service, in a shape /run can return as-is."""

    def __init__(self, service: str, type: str, message: str, *,
                 recoverable: bool, status_code: Optional[int] = None, attempts: int = 1):
        super().__init__(f"{service}: {type}: {message}")
        self.service = service
        self.type = type
        self.message = message
        self.recoverable = recoverable
        self.status_code = status_code
        self.attempts = attempts

    def to_dict(self) -> dict:
        return {
            "type": f"{self.service}_{self.type}",
            "message": self.message,
            "recoverable": self.recoverable,
            "status_code": self.status_code,
            "attempts": self.attempts,
        }


def should_retry(status_code: Optional[int]) -> bool:
    """Decide whether a failed attempt is worth repeating.

    status_code is the HTTP status of the failed response, or None when no
    response arrived at all (timeout, connection refused, DNS failure).
    """
    if status_code is None:
        return True  # timeout or refused connection: the service may just be slow or starting
    # 408 timeout, 429 rate limit, 502/503/504 gateway/unavailable are transient.
    # 4xx client errors (bad key, bad org ID) and 500 (often a deterministic SQL
    # or request error) would fail the same way again.
    return status_code in (408, 429, 502, 503, 504)


def _error_type(status_code: Optional[int], exc: Optional[Exception]) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if exc is not None:
        return "unreachable"
    if status_code in (401, 403):
        return "auth"
    if status_code == 400:
        return "bad_request"
    if status_code == 429:
        return "rate_limited"
    return "http_error"


def request(client: httpx.Client, method: str, url: str, *, service: str,
            max_retries: int, **kwargs: Any) -> httpx.Response:
    """Send a request, retrying at most max_retries times on retryable failures.

    Returns a 2xx response or raises AdapterError. Never raises a raw httpx error.
    """
    attempts = 0
    while True:
        attempts += 1
        response: Optional[httpx.Response] = None
        exc: Optional[Exception] = None
        try:
            response = client.request(method, url, **kwargs)
            if response.is_success:
                return response
        except httpx.HTTPError as e:
            exc = e

        status = response.status_code if response is not None else None
        retryable = should_retry(status)
        if not retryable or attempts > max_retries:
            if exc is not None:
                detail = f"{type(exc).__name__}: {exc}"
            else:
                detail = f"HTTP {status}: {response.text[:300]}"
            raise AdapterError(service, _error_type(status, exc), detail,
                               recoverable=retryable, status_code=status, attempts=attempts)
        time.sleep(BACKOFF_BASE_S * 2 ** (attempts - 1))


def json_body(response: httpx.Response, service: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise AdapterError(service, "invalid_response",
                           f"expected JSON, got: {response.text[:200]!r}",
                           recoverable=False, status_code=response.status_code) from None
