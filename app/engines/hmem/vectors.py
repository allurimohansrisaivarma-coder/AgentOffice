"""Text vectors for H-MEM's semantic and keyword signals.

The `semantic` signal uses a hashed bag-of-words + bigram vector (cosine), not
a neural embedding: no embedding model is called, so retrieval works offline
and the engine never calls a model itself.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

DIM = 1024
_STOP = frozenset("a an and are as at be by for from has have in is it its of on or that the this to was were "
                  "with which who what when where why how we our their they".split())


def tokens(text: str) -> list[str]:
    out = []
    for t in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(t) < 2 or t in _STOP:
            continue
        out.append(t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t)
    return out


def top_tokens(text: str, n: int = 8) -> list[str]:
    """The query's n most frequent tokens (first occurrence breaks ties)."""
    counts = Counter(tokens(text))
    order = {t: i for i, t in reversed(list(enumerate(tokens(text))))}
    return sorted(counts, key=lambda t: (-counts[t], order[t]))[:n]


def _bucket(feature: str) -> int:
    return int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=4).digest(), "big") % DIM


def embed(text: str) -> dict[str, float]:
    toks = tokens(text)
    features = Counter(toks) + Counter(f"{a} {b}" for a, b in zip(toks, toks[1:]))
    vec: dict[int, float] = {}
    for feature, count in features.items():
        b = _bucket(feature)
        vec[b] = vec.get(b, 0.0) + 1.0 + math.log(count)
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {str(k): round(v / norm, 6) for k, v in sorted(vec.items())}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return max(0.0, min(1.0, sum(v * b.get(k, 0.0) for k, v in a.items())))


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))
