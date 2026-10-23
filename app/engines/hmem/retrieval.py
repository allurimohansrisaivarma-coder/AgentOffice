"""MemoryRetrievalService: 8-signal hybrid ranking with trace metadata.

    raw   = 0.35 semantic + 0.20 keyword + 0.10 tag + 0.10 domain + 0.15 entity
            + 0.05 layer + 0.05 recency - 0.20 superseded
    score = clamp01(raw) * confidence          (kept if score >= threshold, default 0.35; top 5)

semantic: cosine of hashed bag-of-words vectors (see vectors.py; not a neural
embedding). keyword: Jaccard of the query's top-8 tokens and the memory's
tokens. layer: long_term 1.0, working 0.8, short_term 0.6. recency:
exp(-days since lastAccessed / 14). The payload is structured, grouped by
domain, and every memory carries its signals and a retrievalReason.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterable, Optional

from app.engines.hmem import vectors
from app.engines.hmem.store import MemoryStoreService

WEIGHTS = {"semantic": 0.35, "keyword": 0.20, "tag": 0.10, "domain": 0.10, "entity": 0.15, "layer": 0.05,
           "recency": 0.05}
SUPERSEDED_PENALTY = 0.20
LAYER_WEIGHT = {"long_term": 1.0, "working": 0.8, "short_term": 0.6}
LAYER_MARK = {"long_term": "LT", "working": "WK", "short_term": "ST"}
RECENCY_DAYS = 14.0


def _age_days(ts: str, now: datetime) -> float:
    then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return max(0.0, (now - then).total_seconds() / 86400)


class MemoryRetrievalService:
    def __init__(self, store: MemoryStoreService):
        self.store = store

    def score(self, memory: dict, *, query_vec: dict, query_tokens: list[str], tags: set, domains: set,
              entities: set, now: datetime) -> dict:
        signals = {
            "semantic": vectors.cosine(query_vec, memory["embedding"]),
            "keyword": vectors.jaccard(query_tokens, vectors.tokens(memory["content"])),
            "tag": vectors.jaccard(tags, memory["tags"]) if tags else 0.0,
            "domain": 1.0 if memory["domain"] in domains else 0.0,
            "entity": len(entities & set(memory["entities"])) / len(entities) if entities else 0.0,
            "layer": LAYER_WEIGHT[memory["layer"]],
            "recency": math.exp(-_age_days(memory["lastAccessed"], now) / RECENCY_DAYS),
        }
        raw = sum(WEIGHTS[k] * v for k, v in signals.items()) - (SUPERSEDED_PENALTY if memory["supersededBy"] else 0)
        signals = {k: round(v, 3) for k, v in signals.items()}
        signals["superseded"] = 1.0 if memory["supersededBy"] else 0.0
        return {"signals": signals, "raw": round(raw, 4),
                "score": round(max(0.0, min(1.0, raw)) * memory["confidence"], 4)}

    def retrieve(self, query: str, *, domains: Iterable[str] = (), entities: Iterable[str] = (),
                 tags: Iterable[str] = (), namespace: Optional[str] = None, top_n: int = 5,
                 threshold: float = 0.35, strategy: str = "comprehensive", actor: str = "MemoryRetrieval",
                 record_reads: bool = True) -> dict:
        domains, entities, tags = set(domains), set(entities), {t.lower() for t in tags}
        query_vec = vectors.embed(f"{query} {' '.join(tags)}")
        query_tokens = vectors.top_tokens(query, 8)
        now = datetime.now(timezone.utc)
        candidates = [m for m in self.store.all(include_superseded=True)
                      if namespace is None or m["namespace"] == namespace]
        ranked = []
        for m in candidates:
            s = self.score(m, query_vec=query_vec, query_tokens=query_tokens, tags=tags, domains=domains,
                           entities=entities, now=now)
            if s["score"] >= threshold:
                ranked.append((m, s))
        ranked.sort(key=lambda pair: (-pair[1]["score"], pair[0]["id"]))
        ranked = ranked[:top_n]

        memories, sections = [], {}
        for m, s in ranked:
            strongest = sorted(((WEIGHTS[k] * s["signals"][k], k) for k in WEIGHTS), reverse=True)[:3]
            reason = ", ".join(f"{k} {s['signals'][k]:.2f}" for _, k in strongest)
            if m["domain"] in domains:
                reason = f"domain '{m['domain']}' requested; " + reason
            memories.append({"id": m["id"], "content": m["content"], "type": m["type"], "layer": m["layer"],
                             "domain": m["domain"], "entities": m["entities"], "tags": m["tags"],
                             "confidence": m["confidence"], "facts": m["facts"], "source": m["source"],
                             "score": s["score"], "signals": s["signals"], "retrievalReason": reason})
            line = f"[{LAYER_MARK[m['layer']]}] {m['content']}"
            if m["entities"]:
                line += f" ({', '.join(m['entities'][:3])})"
            sections.setdefault(m["domain"], []).append(line)
        if record_reads and memories:
            self.store.touch([m["id"] for m in memories], actor=actor, reason=f"retrieved for: {query[:120]}")
        text = "\n".join(f"## {domain}\n" + "\n".join(lines) for domain, lines in sections.items())
        return {"strategy": strategy, "query": query, "threshold": threshold, "top_n": top_n,
                "considered": len(candidates), "memories": memories, "sections": sections, "text": text,
                "weights": WEIGHTS}
