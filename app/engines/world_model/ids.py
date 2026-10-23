"""Stable, content-derived IDs, so independent processes agree on identity.

Name-keyed entities: ent_<first 16 hex of sha256("type:normalized name")>.
Externally keyed entities: type:system:external_id.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def short_hash(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def normalize_name(name: Any) -> str:
    """Casefold, strip diacritics and collapse whitespace: 'José  García' -> 'jose garcia'.

    Getting this wrong silently creates duplicate entities.
    """
    decomposed = unicodedata.normalize("NFKD", str(name))
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(without_marks.casefold().split())


def normalize_type(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", normalize_name(value)).strip("_")


def entity_id(type: str, name: Optional[str] = None, *, system: Optional[str] = None,
              external_id: Optional[Any] = None) -> str:
    t = normalize_type(type)
    if not t:
        raise ValueError("entity type is required")
    if external_id is not None and str(external_id).strip():
        if not system:
            raise ValueError("an external id needs the system it comes from")
        return f"{t}:{normalize_type(system)}:{str(external_id).strip()}"
    key = normalize_name(name or "")
    if not key:
        raise ValueError("entity name is required")
    return "ent_" + short_hash(f"{t}:{key}")


def relationship_id(src_id: str, rel_type: str, dst_id: str, directed: bool = True) -> str:
    a, b = (src_id, dst_id) if directed else tuple(sorted((src_id, dst_id)))
    return "rel_" + short_hash(f"{a}|{normalize_type(rel_type)}|{b}|{int(directed)}")


def context_id(kind: str, name: str) -> str:
    return "ctx_" + short_hash(f"{normalize_type(kind)}:{normalize_name(name)}")


def observation_id(content: dict) -> str:
    """Content-addressed dedupe key: the same fact from the same rows is stored once."""
    return "obs_" + short_hash(canonical_json(content), 24)


def utc_now() -> str:
    return format_ts(datetime.now(timezone.utc))


def format_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def normalize_ts(value: Any) -> str:
    """Any ISO date/datetime (naive = UTC) -> 'YYYY-MM-DDTHH:MM:SS.mmmZ', so strings sort in time order."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"not an ISO timestamp: {value!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_ts(dt.astimezone(timezone.utc))
