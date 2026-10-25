"""WorldStoreService: the canonical store plus its append-only observation log.

SQLite (stdlib), one connection guarded by one re-entrant lock, so there is
exactly one writer at a time and no update is lost. Everything canonical
(entities, relationships, aggregates) is derived from the observation log by
IngestPipelineService; the write methods below are for the pipeline only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union

from app.engines.world_model.ids import canonical_json, entity_id, normalize_name, normalize_type, short_hash, utc_now

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    batch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    confidence REAL NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_subject ON observations(subject_id);
CREATE INDEX IF NOT EXISTS observations_source ON observations(source_key);
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
    stale INTEGER NOT NULL, conflict_count INTEGER NOT NULL,
    version INTEGER NOT NULL, content_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS entities_type ON entities(type);
CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY, src_id TEXT NOT NULL, rel_type TEXT NOT NULL, dst_id TEXT NOT NULL,
    stale INTEGER NOT NULL, conflict_count INTEGER NOT NULL,
    version INTEGER NOT NULL, content_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS relationships_src ON relationships(src_id);
CREATE INDEX IF NOT EXISTS relationships_dst ON relationships(dst_id);
CREATE TABLE IF NOT EXISTS contexts (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subject_contexts (
    subject_id TEXT NOT NULL, context_id TEXT NOT NULL, PRIMARY KEY (subject_id, context_id)
);
CREATE TABLE IF NOT EXISTS aliases (
    type TEXT NOT NULL, alias TEXT NOT NULL, entity_id TEXT NOT NULL, PRIMARY KEY (type, alias)
);
CREATE TABLE IF NOT EXISTS source_items (
    source_key TEXT NOT NULL, row_ref TEXT NOT NULL, subject_id TEXT NOT NULL,
    PRIMARY KEY (source_key, row_ref, subject_id)
);
CREATE TABLE IF NOT EXISTS merge_proposals (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, entity_a TEXT NOT NULL, entity_b TEXT NOT NULL,
    similarity REAL NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patches (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, subject_id TEXT NOT NULL,
    kind TEXT NOT NULL, op TEXT NOT NULL, version INTEGER NOT NULL, changed TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_jobs (
    batch_id TEXT PRIMARY KEY, status TEXT NOT NULL, snapshot_of TEXT,
    plan TEXT NOT NULL, result TEXT, started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS aggregates (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_TABLE = {"entity": "entities", "relationship": "relationships"}
_UNHASHED = ("version", "updated_at")


def _record_hash(record: dict) -> str:
    return short_hash(canonical_json({k: v for k, v in record.items() if k not in _UNHASHED}), 32)


class WorldStoreService:
    def __init__(self, path: Union[str, Path] = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self.lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(f"world store {self.path} is schema v{row['value']}, this code expects "
                                   f"v{SCHEMA_VERSION}, and no migration exists yet")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        with self.lock:
            self._conn.close()

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self._conn.execute(sql, tuple(params))

    # ---------------------------------------------------------------- read surface

    @property
    def schema_version(self) -> int:
        return int(self._one("SELECT value FROM meta WHERE key='schema_version'")["value"])

    def get_entity(self, entity_id: str) -> Optional[dict]:
        row = self._one("SELECT record FROM entities WHERE id=?", (entity_id,))
        return json.loads(row["record"]) if row else None

    def get_context(self, context_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM contexts WHERE id=?", (context_id,))
        if row is None:
            return None
        entities = [r["subject_id"] for r in self._rows(
            "SELECT sc.subject_id FROM subject_contexts sc JOIN entities e ON e.id = sc.subject_id "
            "WHERE sc.context_id=? ORDER BY sc.subject_id", (context_id,))]
        relationships = [r["subject_id"] for r in self._rows(
            "SELECT sc.subject_id FROM subject_contexts sc JOIN relationships r ON r.id = sc.subject_id "
            "WHERE sc.context_id=? ORDER BY sc.subject_id", (context_id,))]
        return {"id": row["id"], "kind": row["kind"], "name": row["name"],
                "first_seen_at": row["first_seen_at"],
                "entities": entities, "relationships": relationships}

    def lookup_id(self, type: str, name: str) -> str:
        """The ID a (type, name) resolves to: a registered alias, else the stable hash."""
        target = self.alias_target(normalize_type(type), normalize_name(name))
        return target or entity_id(type, name)

    def resolve(self, type: str, name: str) -> Optional[dict]:
        return self.get_entity(self.lookup_id(type, name))

    def find_entities(self, *, types: Iterable[str] = (), context_id: Optional[str] = None,
                      include_stale: bool = False, limit: Optional[int] = None) -> list[dict]:
        sql, params = "SELECT e.record FROM entities e", []
        where = []
        if context_id:
            sql += " JOIN subject_contexts sc ON sc.subject_id = e.id"
            where.append("sc.context_id=?")
            params.append(context_id)
        types = [normalize_type(t) for t in types]
        if types:
            where.append(f"e.type IN ({','.join('?' * len(types))})")
            params.extend(types)
        if not include_stale:
            where.append("e.stale=0")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [json.loads(r["record"]) for r in self._rows(sql, params)]

    def relationships_of(self, entity_id: str, *, rel_type: Optional[str] = None,
                         direction: str = "both", include_stale: bool = False) -> list[dict]:
        clauses = {"out": "src_id=?", "in": "dst_id=?", "both": "(src_id=? OR dst_id=?)"}[direction]
        params: list[Any] = [entity_id] * clauses.count("?")
        sql = f"SELECT record FROM relationships WHERE {clauses}"
        if rel_type:
            sql += " AND rel_type=?"
            params.append(normalize_type(rel_type))
        if not include_stale:
            sql += " AND stale=0"
        return [json.loads(r["record"]) for r in self._rows(sql + " ORDER BY id", params)]

    def provenance(self, subject_id: str) -> list[dict]:
        """Every observation behind a record, oldest first, back to source table rows."""
        out = []
        for r in self._rows("SELECT id, source_key, observed_at, confidence, body FROM observations "
                            "WHERE subject_id=? ORDER BY observed_at, seq", (subject_id,)):
            body = json.loads(r["body"])
            out.append({"observation_id": r["id"], "source_key": r["source_key"],
                        "source": body["source"], "observed_at": r["observed_at"],
                        "confidence": r["confidence"]})
        return out

    def history(self, subject_id: str, attribute: str) -> list[dict]:
        """Sparkline projection: one attribute's observed values over time."""
        out = []
        for obs in self.observations_for(subject_id):
            value = (obs.get("entity") or obs.get("relationship") or {}).get("attributes", {}).get(attribute)
            if value is not None:
                out.append({"observed_at": obs["observed_at"], "value": value,
                            "source_key": obs["source_key"], "observation_id": obs["id"]})
        return out

    def subjects_for_source_item(self, source_key: str, row_ref: Any) -> list[str]:
        return [r["subject_id"] for r in self._rows(
            "SELECT subject_id FROM source_items WHERE source_key=? AND row_ref=? ORDER BY subject_id",
            (source_key, canonical_json(row_ref)))]

    def conflicts(self, *, types: Iterable[str] = ()) -> list[dict]:
        out = []
        for e in self.find_entities(types=types, include_stale=True):
            for c in e.get("conflicts", []):
                out.append({"subject_id": e["id"], "type": e["type"], "name": e["name"], **c})
        return out

    def merge_proposals(self, status: Optional[str] = "open") -> list[dict]:
        sql, params = "SELECT * FROM merge_proposals", []
        if status:
            sql, params = sql + " WHERE status=?", [status]
        return [dict(r) for r in self._rows(sql + " ORDER BY similarity DESC, id", params)]

    def patches(self, *, since_seq: int = 0, batch_id: Optional[str] = None) -> list[dict]:
        sql, params = "SELECT * FROM patches WHERE seq>?", [since_seq]
        if batch_id:
            sql += " AND batch_id=?"
            params.append(batch_id)
        return [{**dict(r), "changed": json.loads(r["changed"])} for r in self._rows(sql + " ORDER BY seq", params)]

    def stats(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self._rows("SELECT * FROM aggregates ORDER BY key")}

    def get_job(self, batch_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM ingest_jobs WHERE batch_id=?", (batch_id,))
        if row is None:
            return None
        return {**dict(row), "plan": json.loads(row["plan"]),
                "result": json.loads(row["result"]) if row["result"] else None}

    # ------------------------------------------------- pipeline-only write surface

    def existing_observation_ids(self, ids: Iterable[str]) -> set[str]:
        ids = list(ids)
        found: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            found.update(r["id"] for r in self._rows(
                f"SELECT id FROM observations WHERE id IN ({','.join('?' * len(chunk))})", chunk))
        return found

    def append_observation(self, obs: dict, batch_id: str) -> None:
        body = {k: v for k, v in obs.items() if k not in ("seq", "batch_id")}
        self._exec("INSERT INTO observations (id, batch_id, kind, subject_id, source_key, observed_at, "
                   "confidence, body) VALUES (?,?,?,?,?,?,?,?)",
                   (obs["id"], batch_id, obs["kind"], obs["subject_id"], obs["source_key"],
                    obs["observed_at"], obs["confidence"], canonical_json(body)))

    def observations_for(self, subject_id: str) -> list[dict]:
        return [{**json.loads(r["body"]), "seq": r["seq"], "batch_id": r["batch_id"]}
                for r in self._rows("SELECT seq, batch_id, body FROM observations WHERE subject_id=? "
                                    "ORDER BY observed_at, seq", (subject_id,))]

    def alias_target(self, type: str, alias: str) -> Optional[str]:
        row = self._one("SELECT entity_id FROM aliases WHERE type=? AND alias=?", (type, alias))
        return row["entity_id"] if row else None

    def put_alias(self, type: str, alias: str, entity_id: str) -> None:
        self._exec("INSERT OR IGNORE INTO aliases VALUES (?,?,?)", (type, alias, entity_id))

    def put_context(self, context_id: str, kind: str, name: str, at: str) -> None:
        self._exec("INSERT OR IGNORE INTO contexts VALUES (?,?,?,?)", (context_id, kind, name, at))

    def set_subject_contexts(self, subject_id: str, context_ids: Iterable[str]) -> None:
        self._exec("DELETE FROM subject_contexts WHERE subject_id=?", (subject_id,))
        for cid in sorted(set(context_ids)):
            self._exec("INSERT INTO subject_contexts VALUES (?,?)", (subject_id, cid))

    def read_record(self, kind: str, subject_id: str) -> Optional[dict]:
        row = self._one(f"SELECT record FROM {_TABLE[kind]} WHERE id=?", (subject_id,))
        return json.loads(row["record"]) if row else None

    def write_record(self, kind: str, record: dict, *, batch_id: str, op: str) -> Optional[dict]:
        """Upsert a canonical record. Returns the diff entry, or None if nothing changed."""
        old = self.read_record(kind, record["id"])
        new_hash = _record_hash(record)
        if old is not None and _record_hash(old) == new_hash:
            return None
        version = (old["version"] if old else 0) + 1
        now = utc_now()
        record = {**record, "version": version, "updated_at": now}
        if old is None:
            changed = sorted(k for k in record if k not in _UNHASHED)
            op = "create" if op == "update" else op
        else:
            changed = sorted(k for k in set(old) | set(record)
                             if k not in _UNHASHED and old.get(k) != record.get(k))
        conflict_count = len(record.get("conflicts", []))
        if kind == "entity":
            self._exec("INSERT OR REPLACE INTO entities VALUES (?,?,?,?,?,?,?,?,?)",
                       (record["id"], record["type"], record["name"], int(record["stale"]),
                        conflict_count, version, new_hash, now, canonical_json(record)))
        else:
            self._exec("INSERT OR REPLACE INTO relationships VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (record["id"], record["src_id"], record["rel_type"], record["dst_id"],
                        int(record["stale"]), conflict_count, version, new_hash, now, canonical_json(record)))
        self._exec("INSERT INTO patches (batch_id, subject_id, kind, op, version, changed, at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (batch_id, record["id"], kind, op, version, canonical_json(changed), now))
        return {"subject_id": record["id"], "kind": kind, "op": op, "version": version, "changed": changed}

    def add_source_items(self, source_key: str, row_refs: Iterable[Any], subject_id: str) -> None:
        for ref in row_refs:
            self._exec("INSERT OR IGNORE INTO source_items VALUES (?,?,?)",
                       (source_key, canonical_json(ref), subject_id))

    def add_merge_proposal(self, type: str, a: str, b: str, similarity: float, reason: str) -> bool:
        first, second = sorted((a, b))
        pid = "merge_" + short_hash(f"{first}|{second}")
        cur = self._exec("INSERT OR IGNORE INTO merge_proposals VALUES (?,?,?,?,?,?,?,?)",
                         (pid, type, first, second, similarity, reason, "open", utc_now()))
        return cur.rowcount > 0

    def entity_names(self, type: str) -> list[tuple[str, str]]:
        return [(r["id"], r["name"]) for r in self._rows(
            "SELECT id, name FROM entities WHERE type=? ORDER BY id", (type,))]

    def subjects_from_source(self, source_key: str) -> list[str]:
        return [r["subject_id"] for r in self._rows(
            "SELECT DISTINCT subject_id FROM observations WHERE source_key=? ORDER BY subject_id", (source_key,))]

    def put_job(self, batch_id: str, *, status: str, snapshot_of: Optional[str], plan: dict,
                result: Optional[dict] = None) -> None:
        now = utc_now()
        existing = self.get_job(batch_id)
        started = existing["started_at"] if existing else now
        self._exec("INSERT OR REPLACE INTO ingest_jobs VALUES (?,?,?,?,?,?,?)",
                   (batch_id, status, snapshot_of, canonical_json(plan),
                    canonical_json(result) if result is not None else None, started,
                    now if status == "complete" else None))

    def rebuild_aggregates(self) -> dict:
        """Recomputed from the tables every time, never incremented in place."""
        def grouped(sql: str) -> dict:
            return {r[0]: r[1] for r in self._rows(sql)}
        aggregates = {
            "schema_version": self.schema_version,
            "observations": self._one("SELECT COUNT(*) AS n FROM observations")["n"],
            "entities_by_type": grouped("SELECT type, COUNT(*) FROM entities GROUP BY type ORDER BY type"),
            "relationships_by_type": grouped(
                "SELECT rel_type, COUNT(*) FROM relationships GROUP BY rel_type ORDER BY rel_type"),
            "contexts_by_kind": grouped("SELECT kind, COUNT(*) FROM contexts GROUP BY kind ORDER BY kind"),
            "stale": {"entities": self._one("SELECT COUNT(*) AS n FROM entities WHERE stale=1")["n"],
                      "relationships": self._one("SELECT COUNT(*) AS n FROM relationships WHERE stale=1")["n"]},
            "conflicts": self._one("SELECT COALESCE(SUM(conflict_count), 0) AS n FROM entities")["n"]
                         + self._one("SELECT COALESCE(SUM(conflict_count), 0) AS n FROM relationships")["n"],
            "open_merge_proposals": self._one(
                "SELECT COUNT(*) AS n FROM merge_proposals WHERE status='open'")["n"],
            "sources": grouped("SELECT source_key, COUNT(*) FROM observations GROUP BY source_key ORDER BY source_key"),
        }
        self._exec("DELETE FROM aggregates")
        for key, value in aggregates.items():
            self._exec("INSERT INTO aggregates VALUES (?,?)", (key, canonical_json(value)))
        return aggregates
