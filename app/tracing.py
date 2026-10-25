"""Run traces: one JSON object per line to logs/trace-<run_id>.jsonl and stdout.

Each event carries the hash of the previous event, so an edited or deleted line
breaks the chain and `verify_chain` reports where.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, TextIO

SUMMARY_LIMIT = 500
GENESIS_HASH = "0" * 64

# Key shapes worth masking even when we were never told the value.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"nvapi-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
)


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _event_hash(event: dict) -> str:
    body = {k: v for k, v in event.items() if k != "hash"}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Tracer:
    def __init__(self, run_id: str, log_dir: Path, secrets: Iterable[str] = (),
                 stream: Optional[TextIO] = sys.stdout, listener: Optional[Callable[[dict], None]] = None):
        self.run_id = run_id
        self.path = Path(log_dir) / f"trace-{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Longest first, so a secret that contains another is masked whole.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._stream = stream
        self._listener = listener      # e.g. a UI job following the run live; gets the masked event
        self._prev_hash = GENESIS_HASH

    def mask(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***redacted")
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***redacted", text)
        return text

    def _clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, dict):
            return {k: self._clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._clean(v) for v in value]
        return value

    def summarize(self, value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        text = self.mask(text)
        return text if len(text) <= SUMMARY_LIMIT else text[:SUMMARY_LIMIT] + "...[truncated]"

    def event(self, agent_name: str, action: str, *, status: str = "ok",
              input_summary: Any = "", output_summary: Any = "",
              target_agent: Optional[str] = None, model: Optional[str] = None,
              confidence: Optional[float] = None, retry_count: int = 0,
              **extra: Any) -> dict:
        event = {
            "timestamp": _utc_now(),
            "run_id": self.run_id,
            "agent_name": agent_name,
            "action": action,
            "input_summary": self.summarize(input_summary),
            "output_summary": self.summarize(output_summary),
            "target_agent": target_agent,
            "model": model,
            "confidence": confidence,
            "retry_count": retry_count,
            "status": status,
        }
        event.update(self._clean(extra))
        event["prev_hash"] = self._prev_hash
        event["hash"] = _event_hash(event)
        self._prev_hash = event["hash"]

        line = json.dumps(event, ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self._stream is not None:
            print(line, file=self._stream, flush=True)
        if self._listener is not None:
            self._listener(event)
        return event


def verify_chain(path: Path) -> Optional[int]:
    """Return the 1-based line number of the first broken event, or None if intact."""
    prev = GENESIS_HASH
    with Path(path).open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            event = json.loads(line)
            if event.get("prev_hash") != prev or event.get("hash") != _event_hash(event):
                return lineno
            prev = event["hash"]
    return None
