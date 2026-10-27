"""QA's independent expectations: computed from the spec, never from the code.

`expected_output` is the spec's reference semantics. `generate_cases` builds
inputs that exercise every branch the spec defines (pass, each check failing,
check precedence, key normalization, no match, empty key, ambiguous key, and
values just inside / just outside numeric tolerances), plus seeded random mixes.
"""

from __future__ import annotations

import copy
import random
from typing import Any, Optional

from app.automation.spec import check_passes, normalize_key


class MissingField(ValueError):
    pass


def _require(record: dict, fields: dict, side: str) -> None:
    for name in fields:
        if name not in record:
            raise MissingField(f"{side} record is missing field {name!r}")


def expected_output(spec: dict, left: list[dict], right: list[dict]) -> list[dict]:
    for r in right:
        _require(r, spec["right"]["fields"], "right")
    for l in left:
        _require(l, spec["left"]["fields"], "left")
    key, statuses = spec["key"], spec["statuses"]
    steps = key.get("normalize", [])
    index: dict[str, list[dict]] = {}
    for r in right:
        index.setdefault(normalize_key(r[key["right"]], steps), []).append(r)

    out = []
    for l in left:
        k = normalize_key(l[key["left"]], steps)
        matches = index.get(k, []) if k else []
        row = {"left_id": l[spec["left"]["id_field"]], "right_id": None, "status": statuses["no_match"]}
        if len(matches) > 1:
            row["status"] = statuses["ambiguous"]
        elif len(matches) == 1:
            r = matches[0]
            row["right_id"] = r[spec["right"]["id_field"]]
            row["status"] = statuses["pass"]
            for c in spec["checks"]:
                if not check_passes(c, l[c["left"]], r[c["right"]]):
                    row["status"] = c["fail_status"]
                    break
        out.append(row)
    return out


def _value(ftype: str, field: str, i: int) -> Any:
    return 200.0 + 50 * i if ftype == "number" else f"{field.replace('_', ' ').title()} {i}"


def _key_value(field: str, steps: list[str], i: int) -> str:
    return f"{1000 + i}" if "digits_only" in steps else f"{field[:3].upper()}-{1000 + i}"


def _key_variant(value: str, steps: list[str]) -> Optional[str]:
    """A different-looking key that the normalizers should still map to the same key."""
    variant = value
    if "strip" in steps:
        variant = f"  {variant} "
    if "upper" in steps:
        variant = variant.lower()
    elif "lower" in steps:
        variant = variant.upper()
    if "digits_only" in steps or "arabic_digits" in steps:
        if any(ch in variant for ch in "0123456789"):
            variant = variant.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
        else:
            variant = f"#{variant[:2]}-{variant[2:]}"
    if "arabic_text" in steps and "ا" in variant:
        variant = variant.replace("ا", "أ", 1)
    if "arabic_separators" in steps and ("." in variant or "," in variant):
        variant = variant.replace(".", "٫").replace(",", "٬")
    return variant if variant != value else None


def _failing(check: dict, right_value: Any) -> Any:
    if check["op"] in ("equals", "equals_ci"):
        return f"{right_value} Ltd"
    tolerance = check["value"] / 100 * abs(right_value) if check["op"] == "within_pct" else check["value"]
    return right_value + tolerance * 1.5 + 0.01


def _inside(check: dict, right_value: Any) -> Optional[Any]:
    """A value that differs from right_value but should still pass."""
    if check["op"] == "equals_ci":
        return f"  {str(right_value).swapcase()} "
    if check["op"] == "equals":
        return f" {right_value} "
    tolerance = check["value"] / 100 * abs(right_value) if check["op"] == "within_pct" else check["value"]
    return right_value + tolerance * 0.999 if tolerance > 0 else None


def generate_cases(spec: dict, *, seed: int = 0, random_cases: int = 12) -> dict:
    """{"left": [...], "right": [...], "purpose": {left_id: why this case exists}}."""
    rng = random.Random(seed)
    lf, rf = spec["left"]["fields"], spec["right"]["fields"]
    key, steps = spec["key"], spec["key"].get("normalize", [])
    right: list[dict] = []
    for i in range(6):
        r = {f: _value(t, f, i) for f, t in rf.items()}
        r[key["right"]] = _key_value(key["right"], steps, i)
        if spec["right"]["id_field"] != key["right"]:
            r[spec["right"]["id_field"]] = f"R{i}"
        right.append(r)
    # two right records sharing one key -> ambiguous
    for j in (0, 1):
        dup = {f: _value(t, f, 90 + j) for f, t in rf.items()}
        dup[key["right"]] = _key_value(key["right"], steps, 99)
        if spec["right"]["id_field"] != key["right"]:
            dup[spec["right"]["id_field"]] = f"RDUP{j}"
        right.append(dup)

    left: list[dict] = []
    purpose: dict[str, str] = {}

    def add(base: dict, why: str, **overrides) -> None:
        rec = {f: _value(t, f, 50 + len(left)) for f, t in lf.items()}
        rec[key["left"]] = base[key["right"]]
        for c in spec["checks"]:
            rec[c["left"]] = copy.deepcopy(base[c["right"]])
        rec.update(overrides)
        rec[spec["left"]["id_field"]] = f"L{len(left) + 1:03d}"
        left.append(rec)
        purpose[rec[spec["left"]["id_field"]]] = why

    add(right[0], "every check passes")
    variant = _key_variant(right[1][key["right"]], steps)
    if variant:
        add(right[1], "key differs only in ways the normalizers remove", **{key["left"]: variant})
    for i, c in enumerate(spec["checks"]):
        base = right[2 + i % 3]
        add(base, f"check {i} ({c['op']} on {c['left']}) fails", **{c["left"]: _failing(c, base[c["right"]])})
        inside = _inside(c, base[c["right"]])
        if inside is not None:
            add(base, f"check {i} ({c['op']} on {c['left']}) passes with a non-identical value",
                **{c["left"]: inside})
    if len(spec["checks"]) > 1:
        overrides = {c["left"]: _failing(c, right[5][c["right"]]) for c in spec["checks"]}
        add(right[5], "all checks fail: the first check decides", **overrides)
    add(right[0], "key matches no right record", **{key["left"]: _key_value(key["left"], steps, 777)})
    add(right[0], "key is empty", **{key["left"]: ""})
    add(right[6], "key matches two right records")

    for _ in range(random_cases):
        base = rng.choice(right[:6])
        overrides = {}
        for c in spec["checks"]:
            roll = rng.random()
            if roll < 0.3:
                overrides[c["left"]] = _failing(c, base[c["right"]])
            elif roll < 0.5 and _inside(c, base[c["right"]]) is not None:
                overrides[c["left"]] = _inside(c, base[c["right"]])
        if rng.random() < 0.1:
            overrides[key["left"]] = _key_value(key["left"], steps, 500 + rng.randint(0, 99))
        add(base, "seeded random mix", **overrides)
    return {"left": left, "right": right, "purpose": purpose}


def drift_copy(spec: dict, cases: dict) -> dict:
    """The same cases with the left key column renamed: correct code must raise, not classify."""
    old = spec["key"]["left"]
    left = [{(f"{old}_renamed" if k == old else k): v for k, v in rec.items()} for rec in cases["left"]]
    return {"left": left, "right": cases["right"], "renamed": old}
