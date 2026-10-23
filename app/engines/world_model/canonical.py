"""Canonical reconstruction: turn many observations of one thing into one record.

Strategies (chosen per attribute): latest, union, longest_text,
most_confident, provenance_weighted. Disagreement between *sources* (each
source's latest value) is reported as a conflict with its evidence; a single
source updating a value over time is history, not a conflict.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from app.engines.world_model.ids import canonical_json, normalize_name

STRATEGIES = ("latest", "union", "longest_text", "most_confident", "provenance_weighted")


def order_key(obs: dict) -> tuple:
    return (obs["observed_at"], obs.get("seq", 0))


def noisy_or(observations: Iterable[dict]) -> float:
    """Independent sources corroborate: 1 - prod(1 - best confidence per source)."""
    best: dict[str, float] = {}
    for o in observations:
        best[o["source_key"]] = max(best.get(o["source_key"], 0.0), o["confidence"])
    remaining = 1.0
    for c in best.values():
        remaining *= 1.0 - c
    return round(1.0 - remaining, 4)


def _key(value: Any) -> str:
    return canonical_json(value)


def pick(strategy: str, pairs: list[tuple[Any, dict]]) -> Any:
    """pairs are (value, observation) in time order; later wins every tie."""
    if strategy == "latest":
        return pairs[-1][0]
    if strategy == "union":
        seen, out = set(), []
        for value, _ in pairs:
            for item in value if isinstance(value, list) else [value]:
                if _key(item) not in seen:
                    seen.add(_key(item))
                    out.append(item)
        return out
    if strategy == "longest_text":
        best = None
        for value, _ in pairs:
            length = len(value if isinstance(value, str) else _key(value))
            if best is None or length >= best[0]:
                best = (length, value)
        return best[1]
    if strategy == "most_confident":
        best = None
        for value, obs in pairs:
            if best is None or obs["confidence"] >= best[0]:
                best = (obs["confidence"], value)
        return best[1]
    if strategy == "provenance_weighted":
        support: dict[str, dict[str, float]] = {}
        last_pos: dict[str, int] = {}
        values: dict[str, Any] = {}
        for pos, (value, obs) in enumerate(pairs):
            k = _key(value)
            by_source = support.setdefault(k, {})
            by_source[obs["source_key"]] = max(by_source.get(obs["source_key"], 0.0), obs["confidence"])
            last_pos[k], values[k] = pos, value
        winner = max(support, key=lambda k: (round(sum(support[k].values()), 9), last_pos[k]))
        return values[winner]
    raise ValueError(f"unknown strategy {strategy!r}; expected one of {STRATEGIES}")


def _conflict(attribute: str, strategy: str, chosen: Any,
              pairs: list[tuple[Any, dict]]) -> Optional[dict]:
    latest_per_source: dict[str, tuple[Any, dict]] = {}
    for value, obs in pairs:
        latest_per_source[obs["source_key"]] = (value, obs)
    if len({_key(v) for v, _ in latest_per_source.values()}) < 2:
        return None
    candidates: dict[str, dict] = {}
    for source in sorted(latest_per_source):
        value, obs = latest_per_source[source]
        c = candidates.setdefault(_key(value), {"value": value, "sources": [],
                                                 "observation_ids": [], "confidence": 0.0})
        c["sources"].append(source)
        c["observation_ids"].append(obs["id"])
        c["confidence"] = max(c["confidence"], obs["confidence"])
    return {"attribute": attribute, "strategy": strategy, "chosen": chosen,
            "candidates": list(candidates.values())}


def reconstruct(observations: list[dict], values_of: Callable[[dict], Optional[dict]],
                policy: dict[str, str], default: str = "latest") -> tuple[dict, list[dict]]:
    """Return (canonical attributes, conflicts) for one subject."""
    pairs_by_attr: dict[str, list[tuple[Any, dict]]] = {}
    for obs in sorted(observations, key=order_key):
        for attribute, value in (values_of(obs) or {}).items():
            if value is not None:
                pairs_by_attr.setdefault(attribute, []).append((value, obs))

    attributes, conflicts = {}, []
    for attribute in sorted(pairs_by_attr):
        pairs = pairs_by_attr[attribute]
        strategy = policy.get(attribute, default)
        attributes[attribute] = pick(strategy, pairs)
        if strategy != "union":
            conflict = _conflict(attribute, strategy, attributes[attribute], pairs)
            if conflict:
                conflicts.append(conflict)
    return attributes, conflicts


def name_similarity(a: str, b: str) -> float:
    """1 - normalized Levenshtein distance between normalized names."""
    a, b = normalize_name(a), normalize_name(b)
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return round(1.0 - previous[-1] / max(len(a), len(b)), 4)


def lifecycle_state(*, stale: bool, inferred: bool, mention_count: int, confidence: float) -> str:
    if stale:
        return "archived"
    if inferred or (mention_count < 2 and confidence < 0.8):
        return "emerging"
    return "active"
