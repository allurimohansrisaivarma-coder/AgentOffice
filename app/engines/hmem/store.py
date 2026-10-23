"""MemoryStoreService: tiered, conflict-resolved memory that persists across runs.

Write path for every proposed memory:
  1. no source -> rejected by construction (ValueError)
  2. approval boundary (programmable policy) -> only approved proposals become durable
  3. same id (same content, type, domain, namespace) -> reinforce: confidence +0.05,
     accessCount +1, promote one layer (short_term -> working -> long_term)
  4. conflict detection against active memories -> auto-resolve only at decision
     confidence >= 0.70 (the loser gets supersededBy and is penalized, never deleted);
     otherwise both are tagged `review` for a human
  5. persist + trust ledger entry with the new state
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional, Union

from app.engines.hmem import vectors
from app.engines.hmem.ledger import TrustLedgerService
from app.engines.world_model.ids import canonical_json, short_hash, utc_now

TYPES = ("personal", "factual", "preference", "general")
LAYERS = ("short_term", "working", "long_term")
SOURCE_RANK = {"human": 2, "agent": 1, "system": 0}
AUTO_RESOLVE_AT = 0.70
OPPOSING = (("approved", "rejected"), ("approve", "reject"), ("passed", "failed"), ("pass", "fail"),
            ("accepted", "rejected"), ("increase", "decrease"), ("higher", "lower"), ("trust", "distrust"))
_PII = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"                                   # email
                  r"|(?<!\d)(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"  # 555-123-4567
                  r"|\+\d{8,}")                                                    # +4412345678

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, domain TEXT NOT NULL, type TEXT NOT NULL, layer TEXT NOT NULL,
    namespace TEXT NOT NULL, superseded_by TEXT, record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_domain ON memories(domain);
"""
SCHEMA_VERSION = 1


def new_record(content: str, *, type: str, source: dict, confidence: float, domain: str = "general",
               namespace: str = "default", tags: Iterable[str] = (), entities: Iterable[str] = (),
               related_to: Iterable[str] = (), derived_from: Iterable[str] = (), layer: Optional[str] = None,
               facts: Optional[dict] = None, label: str = "", goal_run_id: Optional[str] = None,
               mission_id: Optional[str] = None, chat_id: Optional[str] = None) -> dict:
    """Build a memory record in the reference field layout. Raises if anything mandatory is missing."""
    if not isinstance(source, dict) or not source.get("kind") or not source.get("name"):
        raise ValueError("a memory with no source (kind + name) is rejected by construction")
    if source["kind"] not in SOURCE_RANK:
        raise ValueError(f"source.kind must be one of {sorted(SOURCE_RANK)}")
    if type not in TYPES:
        raise ValueError(f"type must be one of {TYPES}")
    content = " ".join(str(content or "").split())
    if not content:
        raise ValueError("content is required")
    if not 0.0 < float(confidence) <= 1.0:
        raise ValueError("confidence must be in (0, 1]")
    now = utc_now()
    layer = layer or ("long_term" if source["kind"] == "human" else "short_term")
    if layer not in LAYERS:
        raise ValueError(f"layer must be one of {LAYERS}")
    return {
        "id": "mem_" + short_hash(f"{type}|{domain}|{namespace}|{content.lower()}"),
        "content": content, "type": type, "layer": layer,
        "timestamp": now, "lastAccessed": now, "accessCount": 0,
        "tags": sorted({str(t).lower() for t in tags if str(t).strip()}),
        "embedding": vectors.embed(f"{content} {' '.join(tags)}"),
        "confidence": round(float(confidence), 3), "source": dict(source),
        "provenance": {"origin": source["kind"], "learnedAt": now, "label": label or source["name"],
                       "refs": list(derived_from)},
        "chatId": chat_id, "missionId": mission_id, "goalRunId": goal_run_id,
        "domain": domain, "namespace": namespace,
        "entities": list(dict.fromkeys(entities))[:10], "relatedTo": list(dict.fromkeys(related_to))[:5],
        "supersedes": None, "supersededBy": None, "derivedFrom": list(derived_from),
        "facts": dict(facts or {}),
    }


class ApprovalPolicy:
    """The programmable approval boundary: decides which proposals become durable memory."""

    def __init__(self, min_confidence: float = 0.5):
        self.min_confidence = min_confidence

    def decide(self, record: dict) -> tuple[bool, str]:
        if record["source"]["kind"] == "human":
            return True, "human-sourced"
        if record["type"] == "personal":
            return False, "agents may not store personal memories"
        if _PII.search(record["content"]):
            return False, "content looks like personal data (email or phone number)"
        if record["confidence"] < self.min_confidence:
            return False, f"confidence {record['confidence']} is below {self.min_confidence}"
        if not record["derivedFrom"]:
            return False, "no provenance: derivedFrom is empty"
        return True, f"auto-policy: agent memory with provenance and confidence >= {self.min_confidence}"


class ConflictDetectionService:
    @staticmethod
    def _opposing(a: set, b: set) -> bool:
        return any((x in a and y in b) or (y in a and x in b) for x, y in OPPOSING)

    def scan(self, candidate: dict, existing: list[dict]) -> list[dict]:
        found = []
        cand_tokens = set(vectors.tokens(candidate["content"]))
        for m in existing:
            if m["id"] == candidate["id"] or m["supersededBy"]:
                continue
            if m["id"] in candidate["relatedTo"] or candidate["id"] in m["relatedTo"]:
                continue   # explicit relations suppress conflicts
            shared = set(candidate["entities"]) & set(m["entities"])
            same_domain = candidate["domain"] == m["domain"]
            if not shared and not same_domain:
                continue
            similarity = vectors.cosine(candidate["embedding"], m["embedding"])
            reasons = []
            if shared and self._opposing(cand_tokens, set(vectors.tokens(m["content"]))):
                reasons.append("opposing phrases")
            a, b = vectors.numbers(candidate["content"]), vectors.numbers(m["content"])
            if similarity >= 0.5 and a and b and a != b:
                reasons.append("similar memories with conflicting numbers")
            key = candidate["facts"].get("key")
            if shared and key and key == m["facts"].get("key") and candidate["facts"] != m["facts"]:
                reasons.append("same-entity divergent claims")
            if not reasons:
                continue
            winner, loser = self._rank(candidate, m)
            clear = (SOURCE_RANK[winner["source"]["kind"]] != SOURCE_RANK[loser["source"]["kind"]]
                     or abs(winner["confidence"] - loser["confidence"]) >= 0.15
                     or winner["source"]["kind"] == loser["source"]["kind"] == "human")
            confidence = min(0.95, 0.3 + (0.2 if shared else 0) + (0.1 if len(shared) >= 2 else 0)
                             + (0.15 if same_domain else 0) + (0.15 if clear else 0))
            found.append({"with": m["id"], "reasons": reasons, "shared_entities": sorted(shared),
                          "similarity": round(similarity, 3), "decision_confidence": round(confidence, 2),
                          "winner": winner["id"], "loser": loser["id"]})
        return found

    @staticmethod
    def _rank(a: dict, b: dict) -> tuple[dict, dict]:
        key = lambda m: (SOURCE_RANK[m["source"]["kind"]], m["confidence"], m["timestamp"])
        # the candidate is newest; among equal humans the newest decision wins
        if a["source"]["kind"] == b["source"]["kind"] == "human":
            return a, b
        return (a, b) if key(a) >= key(b) else (b, a)


class MemoryStoreService:
    def __init__(self, path: Union[str, Path], ledger: TrustLedgerService, *,
                 policy: Optional[ApprovalPolicy] = None):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.ledger = ledger
        self.policy = policy or ApprovalPolicy()
        self.conflicts = ConflictDetectionService()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(f"memory store {self.path} is schema v{row['value']}, expected v{SCHEMA_VERSION}")

    # ------------------------------------------------------------------ reads

    def get(self, memory_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT record FROM memories WHERE id=?", (memory_id,)).fetchone()
        return json.loads(row["record"]) if row else None

    def all(self, *, include_superseded: bool = False) -> list[dict]:
        sql = "SELECT record FROM memories" + ("" if include_superseded else " WHERE superseded_by IS NULL")
        with self._lock:
            return [json.loads(r["record"]) for r in self._conn.execute(sql + " ORDER BY id")]

    def find(self, *, domain: Optional[str] = None, fact_kind: Optional[str] = None,
             include_superseded: bool = False) -> list[dict]:
        out = []
        for m in self.all(include_superseded=include_superseded):
            if domain and m["domain"] != domain:
                continue
            if fact_kind and m["facts"].get("kind") != fact_kind:
                continue
            out.append(m)
        return sorted(out, key=lambda m: m["timestamp"])

    # ------------------------------------------------------------------ writes

    def _put(self, record: dict) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?)",
                               (record["id"], record["domain"], record["type"], record["layer"],
                                record["namespace"], record["supersededBy"], canonical_json(record)))

    @staticmethod
    def _public(record: dict) -> dict:
        return {k: v for k, v in record.items() if k != "embedding"}

    def propose(self, record: dict, *, actor: str) -> dict:
        with self._lock:
            approved, reason = self.policy.decide(record)
            if not approved:
                self.ledger.append(record["id"], "proposal_rejected", actor, new=self._public(record), reason=reason)
                return {"status": "rejected", "reason": reason, "memory": None, "conflicts": []}

            existing = self.get(record["id"])
            if existing is not None:
                previous = self._public(existing)
                existing["confidence"] = round(min(0.99, existing["confidence"] + 0.05), 3)
                existing["accessCount"] += 1
                existing["lastAccessed"] = utc_now()
                existing["derivedFrom"] = list(dict.fromkeys(existing["derivedFrom"] + record["derivedFrom"]))
                self.ledger.append(existing["id"], "feedback", actor, previous=previous, new=self._public(existing),
                                   verdict="helpful", reason="the same memory was proposed again (reinforced)")
                index = LAYERS.index(existing["layer"])
                if index + 1 < len(LAYERS) and not existing["supersededBy"]:
                    before = existing["layer"]
                    existing["layer"] = LAYERS[index + 1]
                    self.ledger.append(existing["id"], "promote", actor, previous={"layer": before},
                                       new={"layer": existing["layer"]}, reason="reinforced")
                self._put(existing)
                return {"status": "reinforced", "reason": reason, "memory": self._public(existing), "conflicts": []}

            found = self.conflicts.scan(record, self.all())
            self._put(record)
            self.ledger.append(record["id"], "create", actor, new=self._public(record), approval=reason)
            for conflict in found:
                self.ledger.append(record["id"], "conflict_detected", actor, **conflict)
                if conflict["decision_confidence"] >= AUTO_RESOLVE_AT:
                    self._supersede(conflict["loser"], conflict["winner"], actor, conflict)
                else:
                    for mid in (conflict["winner"], conflict["loser"]):
                        m = self.get(mid)
                        if "review" not in m["tags"]:
                            previous = self._public(m)
                            m["tags"] = sorted(m["tags"] + ["review"])
                            self._put(m)
                            self.ledger.append(mid, "update", actor, previous=previous, new=self._public(m),
                                               reason="conflict below the auto-resolve gate: needs human review")
            return {"status": "created", "reason": reason, "memory": self._public(self.get(record["id"])),
                    "conflicts": found}

    def _supersede(self, loser_id: str, winner_id: str, actor: str, conflict: dict) -> None:
        loser, winner = self.get(loser_id), self.get(winner_id)
        previous = {"loser": self._public(loser), "winner": self._public(winner)}
        loser["supersededBy"] = winner_id
        winner["supersedes"] = loser_id
        self._put(loser)
        self._put(winner)
        self.ledger.append(winner_id, "conflict_resolved", actor, previous=previous,
                           new={"loser": self._public(loser), "winner": self._public(winner)},
                           strategy=f"keep {winner_id}", decision_confidence=conflict["decision_confidence"],
                           reasons=conflict["reasons"])

    def feedback(self, memory_id: str, verdict: str, *, actor: str) -> dict:
        deltas = {"helpful": 0.05, "not_helpful": -0.03, "wrong": -0.20}
        if verdict not in deltas:
            raise ValueError(f"verdict must be one of {sorted(deltas)}")
        with self._lock:
            m = self.get(memory_id)
            if m is None:
                raise KeyError(memory_id)
            previous = self._public(m)
            m["confidence"] = round(min(0.99, max(0.01, m["confidence"] + deltas[verdict])), 3)
            if verdict == "helpful":
                m["accessCount"] += 1
            if verdict == "wrong" and "review" not in m["tags"]:
                m["tags"] = sorted(m["tags"] + ["review"])
            self._put(m)
            self.ledger.append(memory_id, "feedback", actor, previous=previous, new=self._public(m), verdict=verdict)
            return self._public(m)

    def touch(self, memory_ids: Iterable[str], *, actor: str, reason: str) -> None:
        """Record retrieval reads: lastAccessed + accessCount, and a `read` ledger entry."""
        with self._lock:
            for mid in memory_ids:
                m = self.get(mid)
                if m is None:
                    continue
                m["lastAccessed"] = utc_now()
                m["accessCount"] += 1
                self._put(m)
                self.ledger.append(mid, "read", actor, reason=reason)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
