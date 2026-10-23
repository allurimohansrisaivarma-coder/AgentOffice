"""TrustLedgerService: the append-only audit trail of every memory change.

One JSON object per line: memoryId, action, actor, timestamp, optional
previousState / newState, details. Each entry carries the previous entry's
hash, so an edited or deleted line is detectable. Never pruned.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from pathlib import Path
from typing import Optional

from app.engines.world_model.ids import canonical_json, utc_now

ACTIONS = ("create", "read", "update", "feedback", "promote", "demote", "merge", "conflict_detected",
           "conflict_resolved", "proposal_rejected")
GENESIS = "0" * 64


class TrustLedgerService:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._last = self._tail_hash()

    def _tail_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)["hash"]
        return last

    def append(self, memory_id: Optional[str], action: str, actor: str, *, previous: Optional[dict] = None,
               new: Optional[dict] = None, **details) -> dict:
        if action not in ACTIONS:
            raise ValueError(f"unknown ledger action {action!r}")
        with self._lock:
            entry = {"memoryId": memory_id, "action": action, "actor": actor, "timestamp": utc_now(),
                     "previousState": previous, "newState": new, "details": details, "prev_hash": self._last}
            entry["hash"] = hashlib.sha256(canonical_json(entry).encode("utf-8")).hexdigest()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            self._last = entry["hash"]
            return entry

    def entries(self, memory_id: Optional[str] = None) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        return [r for r in rows if memory_id is None or r["memoryId"] == memory_id]

    def stats(self) -> dict:
        return dict(Counter(e["action"] for e in self.entries()))

    def verify(self) -> Optional[int]:
        """1-based line of the first broken entry, or None if intact."""
        prev = GENESIS
        for lineno, e in enumerate(self.entries(), start=1):
            body = {k: v for k, v in e.items() if k != "hash"}
            digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if e.get("prev_hash") != prev or e.get("hash") != digest:
                return lineno
            prev = e["hash"]
        return None
