"""Human approval gate: an append-only, hash-chained ledger of requests and decisions.

An approval is bound to the exact code hash and the hash of the QA report that
passed it, so approving cannot silently cover different code. Approving only
records the decision and keeps the approved code under state/automations/;
nothing is deployed or run automatically.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from pathlib import Path
from typing import Optional

from app.config import ROOT
from app.engines.world_model.ids import canonical_json, utc_now

GENESIS = "0" * 64


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _display(path: Path) -> str:
    """Repo-relative when possible, so responses don't reveal the host's directory layout."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


class ApprovalLedger:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.path = self.dir / "approvals.jsonl"
        self.code_dir = self.dir / "automations"
        self._lock = threading.Lock()

    def _entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _append(self, entry: dict) -> dict:
        with self._lock:
            entries = self._entries()
            entry = {**entry, "at": utc_now(), "prev_hash": entries[-1]["hash"] if entries else GENESIS}
            entry["hash"] = sha256(canonical_json(entry))
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return entry

    def request(self, *, run_id: str, automation: str, code: str, report: dict, decision_memo: dict) -> dict:
        code_hash = sha256(code)
        self.code_dir.mkdir(parents=True, exist_ok=True)
        (self.code_dir / f"{code_hash[:16]}.py").write_text(code, encoding="utf-8")
        entry = self._append({"type": "request", "approval_id": "apr_" + uuid.uuid4().hex[:12], "run_id": run_id,
                              "automation": automation, "code_hash": code_hash,
                              "report_hash": sha256(canonical_json(report)), "memo": decision_memo})
        return self.get(entry["approval_id"])

    def decide(self, approval_id: str, *, decision: str, approver: str, note: str = "",
               code_hash: Optional[str] = None) -> dict:
        current = self.get(approval_id)
        if current is None:
            raise KeyError(approval_id)
        if current["status"] != "pending":
            raise ValueError(f"approval {approval_id} is already {current['status']}")
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be 'approve' or 'reject'")
        if code_hash is not None and code_hash != current["code_hash"]:
            raise ValueError("code_hash does not match the code this approval was requested for")
        self._append({"type": "decision", "approval_id": approval_id, "decision": decision,
                      "approver": approver, "note": note, "code_hash": current["code_hash"]})
        return self.get(approval_id)

    def get(self, approval_id: str) -> Optional[dict]:
        state: Optional[dict] = None
        for e in self._entries():
            if e["approval_id"] != approval_id:
                continue
            if e["type"] == "request":
                state = {k: e[k] for k in ("approval_id", "run_id", "automation", "code_hash", "report_hash",
                                           "memo")}
                state.update({"status": "pending", "requested_at": e["at"]})
            elif state is not None:
                state.update({"status": "approved" if e["decision"] == "approve" else "rejected",
                              "approver": e["approver"], "note": e["note"], "decided_at": e["at"]})
        if state is not None:
            code_file = self.code_dir / f"{state['code_hash'][:16]}.py"
            state["code_path"] = _display(code_file) if code_file.exists() else None
        return state

    def verify(self) -> Optional[int]:
        """1-based line of the first broken entry, or None if the chain is intact."""
        prev = GENESIS
        for lineno, e in enumerate(self._entries(), start=1):
            body = {k: v for k, v in e.items() if k != "hash"}
            if e.get("prev_hash") != prev or e.get("hash") != sha256(canonical_json(body)):
                return lineno
            prev = e["hash"]
        return None
