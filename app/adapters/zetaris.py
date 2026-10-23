"""Zetaris Lightning REST adapter: the data layer every finding is grounded in.

Contract (AGENTS.md 5.1): POST {ZETARIS_REST_URL}/sql-editor/sqls/run with
Bearer key, X-Org-ID and a fresh UUID X-Request-ID; ONE statement per call;
rows come back in `.data`.
"""

from __future__ import annotations

import uuid
from typing import Optional

import httpx

from app.adapters.http import AdapterError, json_body, request

SERVICE = "zetaris"
_READ_ONLY_PREFIXES = ("select", "show", "describe", "desc", "explain", "with")


def split_statements(sql: str) -> list[str]:
    """Split on semicolons that sit outside quotes and comments; drop empty pieces."""
    pieces, current, quote, i = [], [], None, 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
            current.append(ch)
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end == -1 else end
            continue
        elif ch == ";":
            pieces.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    pieces.append("".join(current))
    return [p.strip() for p in pieces if p.strip()]


def is_read_only(sql: str) -> bool:
    return sql.lstrip().lower().startswith(_READ_ONLY_PREFIXES)


class ZetarisClient:
    def __init__(self, base_url: str, org_id: str, api_key: str, *,
                 timeout_s: float = 30.0, max_retries: int = 2,
                 transport: Optional[httpx.BaseTransport] = None):
        if not base_url or not api_key:
            raise AdapterError(SERVICE, "not_configured",
                               "ZETARIS_REST_URL and ZETARIS_API_KEY must be set",
                               recoverable=False)
        self.base_url = base_url.rstrip("/")
        self.org_id = org_id
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout_s, transport=transport,
                                    headers={"Authorization": f"Bearer {api_key}"})

    def run_sql(self, sql: str, limit: int = 100) -> list[dict]:
        statements = split_statements(sql)
        if len(statements) != 1:
            raise AdapterError(SERVICE, "bad_request",
                               f"send exactly one SQL statement per call, got {len(statements)}",
                               recoverable=False)
        statement = statements[0]
        # A write that timed out may still have run, so only reads are retried.
        retries = self.max_retries if is_read_only(statement) else 0
        response = request(
            self._client, "POST", f"{self.base_url}/sql-editor/sqls/run",
            service=SERVICE, max_retries=retries,
            headers={"X-Org-ID": self.org_id, "X-Request-ID": str(uuid.uuid4())},
            json={"queryId": str(uuid.uuid4()), "sql": statement,
                  "source": "SqlEditor", "limit": limit},
        )
        body = json_body(response, SERVICE)
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise AdapterError(SERVICE, "invalid_response",
                               f"no `data` list in response: {str(body)[:200]}",
                               recoverable=False, status_code=response.status_code)
        return rows

    def close(self) -> None:
        self._client.close()


def from_settings(settings, transport: Optional[httpx.BaseTransport] = None) -> ZetarisClient:
    return ZetarisClient(settings.zetaris_rest_url, settings.zetaris_org_id,
                         settings.zetaris_api_key, timeout_s=settings.http_timeout_s,
                         max_retries=settings.max_retries, transport=transport)
