"""ContextBuilderService: pack the part of the world an agent needs into a budget.

Returns a structured payload (never a pre-rendered string): entities,
relationships, contexts and conflicts, each entity with its provenance, plus a
`trace` explaining how every entity was selected and what the budget dropped.
Token estimate is ceil(characters / 4) of the JSON payload, a heuristic.
"""

from __future__ import annotations

import math
import re
from typing import Iterable, Optional

from app.engines.world_model.ids import canonical_json, normalize_name
from app.engines.world_model.store import WorldStoreService

_STOPWORDS = frozenset(
    "a an and are as at be by can could did do does for from has have how i in into is it its "
    "many most much of on or our should that the their they this to us was we were what when "
    "where which who why will with would you".split())


def _singular(term: str) -> str:
    """Crude plural folding so 'invoices' finds 'Invoice matching'; applied to both sides."""
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def _terms(text: str) -> list[str]:
    return sorted({_singular(t) for t in re.findall(r"[a-z0-9]+", normalize_name(text))
                   if len(t) >= 2 and t not in _STOPWORDS})


def estimate_tokens(value) -> int:
    return math.ceil(len(canonical_json(value)) / 4)


class ContextBuilderService:
    def __init__(self, store: WorldStoreService):
        self.store = store

    def build(self, query: str = "", *, entity_ids: Iterable[str] = (), types: Iterable[str] = (),
              context_id: Optional[str] = None, depth: int = 1, max_entities: int = 20,
              token_budget: int = 2000, provenance_per_record: int = 3,
              include_stale: bool = False) -> dict:
        depth = max(0, min(int(depth), 3))
        terms = _terms(query)
        explicit = list(dict.fromkeys(entity_ids))
        candidates = self.store.find_entities(types=types, context_id=context_id, include_stale=include_stale)

        scored: dict[str, dict] = {}
        for e in candidates:
            name_terms = set(_terms(" ".join([e["name"], *e["aliases"]])))
            other_text = " ".join([e.get("description") or "", *e["tags"], canonical_json(e["attributes"])])
            other_terms = set(_terms(other_text))
            matched = [t for t in terms if t in name_terms or t in other_terms]
            score = sum(3.0 if t in name_terms else 1.0 for t in matched)
            if not terms and not explicit:
                score = 1.0   # no query: rank everything by evidence strength below
            if score > 0:
                score += 0.5 * e["confidence"] + 0.1 * math.log1p(e["mention_count"])
                scored[e["id"]] = {"entity": e, "score": round(score, 4), "matched": matched, "via": "query"}
        for eid in explicit:
            e = self.store.get_entity(eid)
            if e and (include_stale or not e["stale"]):
                scored[eid] = {"entity": e, "score": 100.0, "matched": [], "via": "requested"}

        seeds = sorted(scored.values(), key=lambda s: (-s["score"], s["entity"]["id"]))[:max_entities]
        selected = {s["entity"]["id"]: s for s in seeds}

        frontier = list(selected)
        for hop in range(1, depth + 1):
            next_frontier = []
            for eid in frontier:
                parent_score = selected[eid]["score"]
                for rel in self.store.relationships_of(eid, include_stale=include_stale):
                    other = rel["dst_id"] if rel["src_id"] == eid else rel["src_id"]
                    if other in selected or len(selected) >= max_entities:
                        continue
                    e = self.store.get_entity(other)
                    if e is None or (e["stale"] and not include_stale):
                        continue
                    selected[other] = {"entity": e, "score": round(parent_score * 0.5 ** hop, 4),
                                       "matched": [], "via": f"hop {hop} via {rel['rel_type']} from {eid}"}
                    next_frontier.append(other)
            frontier = next_frontier

        dropped: list[str] = []
        while True:
            payload = self._payload(query, selected, include_stale, provenance_per_record)
            tokens = estimate_tokens(payload)
            if tokens <= token_budget or len(selected) <= 1:
                break
            weakest = min(selected.values(), key=lambda s: (s["score"], s["entity"]["id"]))
            dropped.append(weakest["entity"]["id"])
            del selected[weakest["entity"]["id"]]

        payload["trace"] = {
            "strategy": f"keyword match on names (x3) and attributes (x1) + 0.5*confidence; "
                        f"{depth}-hop expansion at half score per hop",
            "query_terms": terms,
            "selected": [{"id": eid, "name": s["entity"]["name"], "score": s["score"],
                          "matched": s["matched"], "via": s["via"]}
                         for eid, s in sorted(selected.items(), key=lambda kv: (-kv[1]["score"], kv[0]))],
            "candidates_considered": len(candidates),
            "dropped_for_budget": dropped,
            "token_estimate": tokens,
            "token_budget": token_budget,
            "token_heuristic": "ceil(chars/4) of the JSON payload",
            "schema_version": self.store.schema_version,
        }
        return payload

    def _payload(self, query: str, selected: dict, include_stale: bool, provenance_per_record: int) -> dict:
        ids = set(selected)
        entities, conflicts, context_ids = [], [], set()
        for eid, s in sorted(selected.items(), key=lambda kv: (-kv[1]["score"], kv[0])):
            e = s["entity"]
            context_ids.update(e["contexts"])
            conflicts.extend({"subject_id": eid, "name": e["name"], **c} for c in e["conflicts"])
            entities.append({
                "id": eid, "type": e["type"], "name": e["name"], "aliases": e["aliases"],
                "description": e["description"], "tags": e["tags"], "attributes": e["attributes"],
                "confidence": e["confidence"], "state": e["state"], "mention_count": e["mention_count"],
                "last_seen_at": e["last_seen_at"], "has_conflicts": bool(e["conflicts"]),
                "provenance": [{"source_key": p["source_key"], "rows": p["source"].get("rows"),
                                "observation_id": p["observation_id"]}
                               for p in self.store.provenance(eid)[-provenance_per_record:]],
            })
        relationships, seen = [], set()
        for eid in sorted(ids):
            for rel in self.store.relationships_of(eid, include_stale=include_stale):
                if rel["id"] in seen or rel["src_id"] not in ids or rel["dst_id"] not in ids:
                    continue
                seen.add(rel["id"])
                relationships.append({"id": rel["id"], "src_id": rel["src_id"], "rel_type": rel["rel_type"],
                                      "dst_id": rel["dst_id"], "properties": rel["properties"],
                                      "sources": rel["sources"]})
        contexts = [c for c in (self.store.get_context(cid) for cid in sorted(context_ids)) if c]
        return {"query": query, "entities": entities, "relationships": relationships,
                "contexts": [{"id": c["id"], "kind": c["kind"], "name": c["name"]} for c in contexts],
                "conflicts": conflicts}
