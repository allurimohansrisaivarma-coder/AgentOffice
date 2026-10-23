"""IngestPipelineService: the only writer of the World Model.

Per observation (inside step 1): extract -> normalize -> resolve -> validate.
Per batch, the 10-step aggregate cycle:

    1 reconcile          normalize, resolve stable IDs and aliases, reject anything without provenance
    2 cluster            group by subject, classify new vs already-logged observations
    3 persist_primary    append new observations to the log (the only exactly-once stage), aliases, contexts
    4 score              confidence (noisy-or across sources), mention counts, first/last seen
    5 infer              stub entities for relationship endpoints, merge proposals (never applied)
    6 persist_edges      canonical relationship records
    7 rebuild_views      canonical entity records and context membership
    8 patch_source_items map source rows back to the IDs they produced
    9 rebuild_aggregates world-level counts, recomputed from scratch
   10 mark_stale         for a snapshot batch, archive what the source no longer contains

Step 3 commits on its own. Steps 4-10 are recomputed from the log in a second
transaction, so re-running a batch that was interrupted after step 3 resumes
it, and re-running a finished batch is a no-op with an empty diff.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional, Union

from app.engines.world_model.canonical import (
    STRATEGIES, lifecycle_state, name_similarity, noisy_or, pick, reconstruct,
)
from app.engines.world_model.ids import (
    canonical_json, context_id, entity_id, normalize_name, normalize_ts, normalize_type,
    observation_id, relationship_id, short_hash, utc_now,
)
from app.engines.world_model.store import WorldStoreService

STEPS = ("reconcile", "cluster", "persist_primary", "score", "infer", "persist_edges",
         "rebuild_views", "patch_source_items", "rebuild_aggregates", "mark_stale")

Endpoint = Union[tuple, dict, str]


# ------------------------------------------------------------ observation builders

def zetaris_source(table: str, rows: Iterable[Any], query: Optional[str] = None) -> dict:
    return {"system": "zetaris", "table": table, "rows": list(rows), "query": query}


def agent_source(agent: str, run_id: Optional[str] = None, derived_from: Iterable[str] = ()) -> dict:
    return {"system": "agent", "agent": agent, "run_id": run_id, "derived_from": list(derived_from)}


def entity_observation(type: str, name: str, *, source: dict, attributes: Optional[dict] = None,
                       aliases: Iterable[str] = (), description: Optional[str] = None,
                       tags: Iterable[str] = (), contexts: Iterable[Any] = (), confidence: float = 1.0,
                       observed_at: Optional[str] = None, system: Optional[str] = None,
                       external_id: Optional[Any] = None) -> dict:
    return {"kind": "entity", "type": type, "name": name, "attributes": dict(attributes or {}),
            "aliases": list(aliases), "description": description, "tags": list(tags),
            "contexts": list(contexts), "source": source, "confidence": confidence,
            "observed_at": observed_at, "system": system, "external_id": external_id}


def relationship_observation(src: Endpoint, rel_type: str, dst: Endpoint, *, source: dict,
                             attributes: Optional[dict] = None, weight: Optional[float] = None,
                             directed: bool = True, valid_from: Optional[str] = None,
                             valid_to: Optional[str] = None, contexts: Iterable[Any] = (),
                             confidence: float = 1.0, observed_at: Optional[str] = None) -> dict:
    """src/dst: (type, name), {"type": .., "name": ..} or {"id": ..}."""
    return {"kind": "relationship", "src": src, "rel_type": rel_type, "dst": dst,
            "attributes": dict(attributes or {}), "weight": weight, "directed": directed,
            "valid_from": valid_from, "valid_to": valid_to, "contexts": list(contexts),
            "source": source, "confidence": confidence, "observed_at": observed_at}


# ------------------------------------------------------------ validation helpers

def source_key_and_rows(source: Any) -> tuple[str, list]:
    """Validate provenance. A source table must name the rows a fact came from."""
    if not isinstance(source, dict) or not str(source.get("system") or "").strip():
        raise ValueError("missing provenance: source.system is required")
    system = normalize_type(source["system"])
    if source.get("table"):
        rows = source.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"missing provenance: source table {source['table']!r} needs the rows it came from")
        return f"{system}:{source['table']}", rows
    if system == "agent":
        if not str(source.get("agent") or "").strip():
            raise ValueError("missing provenance: an agent source needs the agent's name")
        return f"agent:{source['agent']}", []
    if not str(source.get("ref") or "").strip():
        raise ValueError("missing provenance: give source.table + rows, an agent, or a ref")
    return f"{system}:{source['ref']}", []


def _clean_name(value: Any) -> str:
    return " ".join(str(value or "").split())


def _context(value: Any) -> dict:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        value = {"kind": value[0], "name": value[1]}
    if not isinstance(value, dict):
        raise ValueError("a context must be (kind, name) or {'kind': .., 'name': ..}")
    kind, name = normalize_type(value.get("kind", "")), _clean_name(value.get("name"))
    if not kind or not name:
        raise ValueError("each context needs a kind and a name")
    return {"id": context_id(kind, name), "kind": kind, "name": name}


def _endpoint(value: Any, label: str) -> dict:
    if isinstance(value, str):
        value = {"id": value}
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        value = {"type": value[0], "name": value[1]}
    if not isinstance(value, dict):
        raise ValueError(f"relationship {label} must be (type, name), {{'type', 'name'}} or {{'id'}}")
    if value.get("id"):
        return {"id": str(value["id"])}
    etype, name = normalize_type(value.get("type", "")), _clean_name(value.get("name"))
    if not etype or not name:
        raise ValueError(f"relationship {label} needs a type and a name")
    return {"type": etype, "name": name}


def _digits(name: str) -> list[str]:
    return re.findall(r"\d+", name)


class IngestPipelineService:
    def __init__(self, store: WorldStoreService, *, policy: Optional[dict] = None,
                 default_strategy: str = "latest", merge_threshold: float = 0.85):
        """policy: {"*": {attr: strategy}, "<entity type or rel:<rel type>>": {attr: strategy}}."""
        self.store = store
        self.policy = {normalize_type(k) if k != "*" else "*": dict(v) for k, v in (policy or {}).items()}
        for attrs in [{"*": default_strategy}] + list(self.policy.values()):
            for strategy in attrs.values():
                if strategy not in STRATEGIES:
                    raise ValueError(f"unknown strategy {strategy!r}; expected one of {STRATEGIES}")
        self.default_strategy = default_strategy
        self.merge_threshold = merge_threshold

    def _policy_for(self, key: str) -> dict:
        return {**self.policy.get("*", {}), **self.policy.get(normalize_type(key), {})}

    # ---------------------------------------------------------------- public API

    def ingest(self, observations: list[dict], *, snapshot_of: Optional[str] = None,
               tracer=None, actor: str = "WorldModel") -> dict:
        with self.store.lock:   # one running ingest per world: concurrent callers queue here
            return self._ingest(observations, snapshot_of, tracer, actor)

    def _ingest(self, observations, snapshot_of, tracer, actor) -> dict:
        steps: list[dict] = []

        def log(step: str, detail: str, status: str = "ok", **extra) -> None:
            steps.append({"step": step, "detail": detail, "status": status})
            if tracer is not None:
                tracer.event(actor, f"world_model.{step}", status=status, output_summary=detail, **extra)

        # 1 reconcile
        accepted, rejected, aliases, alias_conflicts, merge_hints = self._reconcile(observations)
        log("reconcile", f"{len(observations)} received, {len(accepted)} accepted, {len(rejected)} rejected"
                         + (f": {rejected[0]['reason']}" if rejected else ""))
        if not accepted:
            return {"batch_id": None, "status": "empty", "replayed": False, "resumed": False,
                    "received": len(observations), "accepted": 0, "rejected": rejected, "diff": [],
                    "steps": steps}

        batch_id = "batch_" + short_hash(canonical_json([sorted(o["id"] for o in accepted), snapshot_of]), 24)
        job = self.store.get_job(batch_id)
        if job and job["status"] == "complete":
            log("reconcile", f"batch {batch_id} already ingested; only staleness is re-checked",
                batch_id=batch_id)
            # A re-sent snapshot still says "this is all the source holds now", and
            # other batches may have changed staleness since, so step 10 always runs.
            diff: list[dict] = []
            with self.store.transaction():
                stale_changes = self._mark_stale(batch_id, job["plan"], snapshot_of, diff)
                aggregates = self.store.rebuild_aggregates() if stale_changes else job["result"]["aggregates"]
            log("mark_stale", f"{stale_changes} records changed staleness" if snapshot_of
                              else "not a snapshot batch; nothing marked stale", batch_id=batch_id)
            return {**job["result"], "replayed": True, "resumed": False, "diff": diff,
                    "stale_changes": stale_changes, "aggregates": aggregates,
                    "rejected": rejected, "steps": steps}

        # 2 cluster
        if job is None:
            already = self.store.existing_observation_ids(o["id"] for o in accepted)
            new = [o for o in accepted if o["id"] not in already]
            entity_subjects = sorted({o["subject_id"] for o in accepted if o["kind"] == "entity"})
            rel_subjects = sorted({o["subject_id"] for o in accepted if o["kind"] == "relationship"})
            endpoints = sorted({o["relationship"][end]["id"] for o in accepted if o["kind"] == "relationship"
                                for end in ("src", "dst")})
            plan = {
                "entity_subjects": entity_subjects, "relationship_subjects": rel_subjects,
                "endpoints": endpoints,
                "seen": sorted({o["subject_id"] for o in accepted if o["source_key"] == snapshot_of}),
                "new_count": len(new), "duplicate_count": len(accepted) - len(new),
                "alias_conflicts": alias_conflicts, "merge_hints": merge_hints,
            }
            log("cluster", f"{len(entity_subjects)} entities, {len(rel_subjects)} relationships; "
                           f"{len(new)} new observations, {plan['duplicate_count']} already logged",
                batch_id=batch_id)

            # 3 persist_primary (own transaction: the exactly-once stage)
            with self.store.transaction():
                for o in new:
                    self.store.append_observation(o, batch_id)
                for (etype, alias), target in aliases.items():
                    self.store.put_alias(etype, alias, target)
                for o in accepted:
                    for c in o["contexts"]:
                        self.store.put_context(c["id"], c["kind"], c["name"], o["observed_at"])
                self.store.put_job(batch_id, status="appended", snapshot_of=snapshot_of, plan=plan)
            log("persist_primary", f"appended {len(new)} observations, {len(aliases)} aliases",
                batch_id=batch_id)
            resumed = False
        else:
            plan = job["plan"]
            resumed = True
            log("cluster", f"resuming batch {batch_id}: its observations were appended before an interruption",
                batch_id=batch_id)

        # 4-10 derive, all recomputed from the log
        try:
            with self.store.transaction():
                result = self._derive(batch_id, plan, accepted, snapshot_of, log)
                result.update({"resumed": resumed, "received": len(observations), "rejected": rejected})
                self.store.put_job(batch_id, status="complete", snapshot_of=snapshot_of, plan=plan,
                                   result={k: v for k, v in result.items() if k != "steps"})
        except Exception as e:
            log("rollback", f"derive steps rolled back ({type(e).__name__}: {e}); the appended "
                            f"observations are kept, so re-running this batch resumes it",
                status="error", batch_id=batch_id)
            raise
        result["steps"] = steps
        return result

    # ---------------------------------------------------------------- step 1

    def _normalize(self, raw: Any, ingest_ts: str) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("an observation must be an object")
        source_key, _ = source_key_and_rows(raw.get("source"))
        try:
            confidence = float(raw.get("confidence", 1.0))
        except (TypeError, ValueError):
            raise ValueError("confidence must be a number") from None
        if not 0.0 < confidence <= 1.0:
            raise ValueError("confidence must be in (0, 1]")
        given_ts = normalize_ts(raw["observed_at"]) if raw.get("observed_at") else None
        contexts = sorted({c["id"]: c for c in map(_context, raw.get("contexts") or [])}.values(),
                          key=lambda c: c["id"])
        obs: dict = {"kind": raw.get("kind"), "contexts": contexts, "source": raw["source"],
                     "confidence": confidence}

        if obs["kind"] == "entity":
            etype, name = normalize_type(raw.get("type", "")), _clean_name(raw.get("name"))
            if not etype:
                raise ValueError("entity type is required")
            if not name:
                raise ValueError("entity name is required")
            external = raw.get("external_id")
            if external is not None and not raw.get("system"):
                raise ValueError("an external id needs the system it comes from")
            attributes = raw.get("attributes") or {}
            if not isinstance(attributes, dict):
                raise ValueError("attributes must be an object")
            obs["entity"] = {
                "type": etype, "name": name,
                "aliases": sorted({_clean_name(a) for a in raw.get("aliases") or [] if _clean_name(a)}),
                "description": raw.get("description") or None,
                "tags": sorted({str(t).strip().lower() for t in raw.get("tags") or [] if str(t).strip()}),
                "attributes": attributes, "system": raw.get("system"),
                "external_id": None if external is None else str(external),
            }
        elif obs["kind"] == "relationship":
            rel_type = normalize_type(raw.get("rel_type", ""))
            if not rel_type:
                raise ValueError("relationship rel_type is required")
            attributes = dict(raw.get("attributes") or {})
            for key in ("weight", "valid_from", "valid_to"):
                if raw.get(key) is not None:
                    attributes[key] = raw[key]
            obs["relationship"] = {"rel_type": rel_type, "src": _endpoint(raw.get("src"), "src"),
                                   "dst": _endpoint(raw.get("dst"), "dst"),
                                   "directed": bool(raw.get("directed", True)), "attributes": attributes}
        else:
            raise ValueError("kind must be 'entity' or 'relationship'")

        obs["id"] = observation_id({**obs, "observed_at": given_ts})
        obs["source_key"] = source_key
        obs["observed_at"] = given_ts or ingest_ts
        return obs

    def _resolve_name(self, etype: str, name: str, batch_aliases: dict) -> str:
        key = normalize_name(name)
        return batch_aliases.get((etype, key)) or self.store.alias_target(etype, key) or entity_id(etype, name)

    def _reconcile(self, observations: list) -> tuple:
        ingest_ts = utc_now()
        normalized, rejected, seen_ids = [], [], set()
        for index, raw in enumerate(observations):
            try:
                obs = self._normalize(raw, ingest_ts)
            except ValueError as e:
                rejected.append({"index": index, "reason": str(e)})
                continue
            if obs["id"] not in seen_ids:     # exact duplicates inside one batch
                seen_ids.add(obs["id"])
                normalized.append(obs)

        # Aliases declared in this batch apply to the rest of this batch.
        batch_aliases: dict = {}
        alias_conflicts, merge_hints = [], []
        for obs in normalized:
            ent = obs.get("entity")
            if not ent or ent["external_id"] is not None:
                continue
            target = self._resolve_name(ent["type"], ent["name"], batch_aliases)
            for alias in ent["aliases"]:
                key = normalize_name(alias)
                if key == normalize_name(ent["name"]):
                    continue
                existing = batch_aliases.get((ent["type"], key)) or self.store.alias_target(ent["type"], key)
                if existing == target:
                    continue
                if existing:
                    alias_conflicts.append({"type": ent["type"], "alias": alias, "claimed_by": target,
                                            "already_points_to": existing})
                    continue
                own_id = entity_id(ent["type"], alias)
                if self.store.read_record("entity", own_id) is not None:
                    merge_hints.append({"type": ent["type"], "a": target, "b": own_id, "reason":
                                        f"{ent['name']!r} declares alias {alias!r}, which is already an entity"})
                    continue
                batch_aliases[(ent["type"], key)] = target

        for obs in normalized:
            if obs["kind"] == "entity":
                ent = obs["entity"]
                obs["subject_id"] = (entity_id(ent["type"], system=ent["system"], external_id=ent["external_id"])
                                     if ent["external_id"] is not None
                                     else self._resolve_name(ent["type"], ent["name"], batch_aliases))
            else:
                rel = obs["relationship"]
                for end in ("src", "dst"):
                    ep = rel[end]
                    if "id" not in ep:
                        ep["id"] = self._resolve_name(ep["type"], ep["name"], batch_aliases)
                obs["subject_id"] = relationship_id(rel["src"]["id"], rel["rel_type"], rel["dst"]["id"],
                                                    rel["directed"])
        return normalized, rejected, batch_aliases, alias_conflicts, merge_hints

    # ---------------------------------------------------------------- steps 4-10

    def _derive(self, batch_id: str, plan: dict, accepted: list, snapshot_of: Optional[str], log) -> dict:
        diff: list[dict] = []
        subjects = sorted(set(plan["entity_subjects"]) | set(plan["endpoints"]) | set(plan["relationship_subjects"]))
        observations = {sid: self.store.observations_for(sid) for sid in subjects}
        observations = {sid: obs for sid, obs in observations.items() if obs}

        scores = self._score(observations)
        log("score", f"scored {len(scores)} subjects from the log", batch_id=batch_id)

        stubs, proposals = self._infer(batch_id, plan, observations, diff)
        log("infer", f"{stubs} inferred endpoint entities, {proposals} new merge proposals", batch_id=batch_id)

        edges = self._persist_edges(batch_id, plan, observations, scores, diff)
        log("persist_edges", f"{edges} relationship records changed", batch_id=batch_id)

        views = self._rebuild_views(batch_id, plan, observations, scores, diff)
        log("rebuild_views", f"{views} entity records changed", batch_id=batch_id)

        items = self._patch_source_items(accepted)
        log("patch_source_items", f"{items} source rows mapped to IDs", batch_id=batch_id)

        aggregates = self.store.rebuild_aggregates()
        log("rebuild_aggregates", f"{aggregates['observations']} observations, "
                                  f"{sum(aggregates['entities_by_type'].values())} entities", batch_id=batch_id)

        stale_changes = self._mark_stale(batch_id, plan, snapshot_of, diff)
        if stale_changes:
            aggregates = self.store.rebuild_aggregates()
        log("mark_stale", f"{stale_changes} records changed staleness" if snapshot_of
                          else "not a snapshot batch; nothing marked stale", batch_id=batch_id)

        return {"batch_id": batch_id, "status": "complete", "replayed": False, "snapshot_of": snapshot_of,
                "accepted": len(accepted), "new_observations": plan["new_count"],
                "duplicates": plan["duplicate_count"], "alias_conflicts": plan["alias_conflicts"],
                "touched": {"entities": len(plan["entity_subjects"]),
                            "relationships": len(plan["relationship_subjects"])},
                "inferred_stubs": stubs, "merge_proposals_added": proposals,
                "stale_changes": stale_changes, "diff": diff, "aggregates": aggregates}

    @staticmethod
    def _score(observations: dict) -> dict:
        return {sid: {"confidence": noisy_or(obs), "mention_count": len(obs),
                      "first_seen_at": obs[0]["observed_at"], "last_seen_at": obs[-1]["observed_at"],
                      "sources": sorted({o["source_key"] for o in obs})}
                for sid, obs in observations.items()}

    def _infer(self, batch_id: str, plan: dict, observations: dict, diff: list) -> tuple[int, int]:
        stubs = 0
        for rid in plan["relationship_subjects"]:
            for obs in observations.get(rid, []):
                rel = obs["relationship"]
                for end in ("src", "dst"):
                    ep = rel[end]
                    if "type" not in ep or ep["id"] in observations or self.store.read_record("entity", ep["id"]):
                        continue
                    stub = {"id": ep["id"], "type": ep["type"], "name": ep["name"], "aliases": [],
                            "description": None, "tags": [], "attributes": {}, "conflicts": [], "contexts": [],
                            "sources": [], "confidence": round(0.5 * obs["confidence"], 4), "mention_count": 0,
                            "first_seen_at": obs["observed_at"], "last_seen_at": obs["observed_at"],
                            "system": None, "external_id": None, "inferred": True, "stale": False,
                            "state": "emerging"}
                    change = self.store.write_record("entity", stub, batch_id=batch_id, op="infer")
                    if change:
                        diff.append(change)
                        stubs += 1

        proposals = 0
        for hint in plan["merge_hints"]:
            proposals += self.store.add_merge_proposal(hint["type"], hint["a"], hint["b"], 1.0, hint["reason"])
        batch_names: dict[str, list[tuple[str, str]]] = {}
        for eid in plan["entity_subjects"]:
            if eid in observations:
                ent = observations[eid][-1]["entity"]
                batch_names.setdefault(ent["type"], []).append((eid, ent["name"]))
        for etype, names in batch_names.items():
            candidates = dict(self.store.entity_names(etype))
            candidates.update(names)
            for eid, name in names:
                for other_id, other_name in candidates.items():
                    if other_id == eid or _digits(name) != _digits(other_name):
                        continue   # "Invoice 101" vs "Invoice 102" are different things
                    similarity = name_similarity(name, other_name)
                    if similarity >= self.merge_threshold:
                        proposals += self.store.add_merge_proposal(
                            etype, eid, other_id, similarity,
                            f"names {name!r} and {other_name!r} are {similarity:.0%} similar")
        return stubs, proposals

    def _persist_edges(self, batch_id: str, plan: dict, observations: dict, scores: dict, diff: list) -> int:
        changed = 0
        for rid in plan["relationship_subjects"]:
            obs = observations[rid]
            rel = obs[-1]["relationship"]
            attributes, conflicts = reconstruct(obs, lambda o: o["relationship"]["attributes"],
                                                self._policy_for("rel:" + rel["rel_type"]), self.default_strategy)
            old = self.store.read_record("relationship", rid)
            score = scores[rid]
            stale = bool(old and old["stale"])
            record = {"id": rid, "src_id": rel["src"]["id"], "rel_type": rel["rel_type"], "dst_id": rel["dst"]["id"],
                      "directed": rel["directed"], "properties": {**attributes, "confidence": score["confidence"]},
                      "conflicts": conflicts, "contexts": sorted({c["id"] for o in obs for c in o["contexts"]}),
                      **score, "inferred": False, "stale": stale,
                      "state": lifecycle_state(stale=stale, inferred=False, mention_count=score["mention_count"],
                                               confidence=score["confidence"])}
            self.store.set_subject_contexts(rid, record["contexts"])
            change = self.store.write_record("relationship", record, batch_id=batch_id, op="update")
            if change:
                diff.append(change)
                changed += 1
        return changed

    def _rebuild_views(self, batch_id: str, plan: dict, observations: dict, scores: dict, diff: list) -> int:
        changed = 0
        for eid in sorted(set(plan["entity_subjects"]) | set(plan["endpoints"])):
            obs = [o for o in observations.get(eid, []) if o["kind"] == "entity"]
            if not obs:
                continue   # a stub endpoint: written by infer, nothing observed yet
            latest = obs[-1]["entity"]
            attributes, conflicts = reconstruct(obs, lambda o: o["entity"]["attributes"],
                                                self._policy_for(latest["type"]), self.default_strategy)
            descriptions = [(o["entity"]["description"], o) for o in obs if o["entity"]["description"]]
            canonical_key = normalize_name(latest["name"])
            aliases: dict[str, str] = {}
            for o in obs:
                for n in [o["entity"]["name"], *o["entity"]["aliases"]]:
                    if normalize_name(n) != canonical_key:
                        aliases.setdefault(normalize_name(n), n)
            old = self.store.read_record("entity", eid)
            score = scores[eid]
            stale = bool(old and old["stale"])
            record = {"id": eid, "type": latest["type"], "name": latest["name"],
                      "aliases": [aliases[k] for k in sorted(aliases)],
                      "description": pick("longest_text", descriptions) if descriptions else None,
                      "tags": sorted({t for o in obs for t in o["entity"]["tags"]}),
                      "attributes": attributes, "conflicts": conflicts,
                      "contexts": sorted({c["id"] for o in obs for c in o["contexts"]}), **score,
                      "system": latest["system"], "external_id": latest["external_id"],
                      "inferred": False, "stale": stale,
                      "state": lifecycle_state(stale=stale, inferred=False, mention_count=score["mention_count"],
                                               confidence=score["confidence"])}
            self.store.set_subject_contexts(eid, record["contexts"])
            change = self.store.write_record("entity", record, batch_id=batch_id, op="update")
            if change:
                diff.append(change)
                changed += 1
        return changed

    def _patch_source_items(self, accepted: list) -> int:
        mapped = 0
        for obs in accepted:
            _, rows = source_key_and_rows(obs["source"])
            if rows:
                self.store.add_source_items(obs["source_key"], rows, obs["subject_id"])
                mapped += len(rows)
        return mapped

    def _mark_stale(self, batch_id: str, plan: dict, snapshot_of: Optional[str], diff: list) -> int:
        if not snapshot_of:
            return 0
        seen = set(plan["seen"])
        changed = 0
        for sid in self.store.subjects_from_source(snapshot_of):
            kind = "entity" if self.store.read_record("entity", sid) else "relationship"
            record = self.store.read_record(kind, sid)
            want_stale = sid not in seen
            if record is None or record["stale"] == want_stale:
                continue
            record["stale"] = want_stale
            record["state"] = lifecycle_state(stale=want_stale, inferred=record.get("inferred", False),
                                              mention_count=record["mention_count"],
                                              confidence=record["confidence"])
            change = self.store.write_record(kind, record, batch_id=batch_id,
                                             op="stale" if want_stale else "unstale")
            if change:
                diff.append(change)
                changed += 1
        return changed
