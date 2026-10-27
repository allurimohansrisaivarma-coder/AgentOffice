"""QA for spreadsheet automations: test cases from the spec and the real file, judged by the oracle.

`build_cases` mixes real rows from the uploaded file, single-cell mutations of
every declared input column (blank, whitespace, case, numbers typed as text)
and cases aimed at each step's edge rules (lookup misses and duplicates, date
formats, thresholds, duplicates, blanks in sorts and groups). Expected output
always comes from app/tasks/runtime.py, never from the code under test.
`compile_spec` is sample mode's Engineer: it embeds the runtime in a standalone
module, so offline code is correct by construction and says so in its origin.
"""

from __future__ import annotations

import copy
import json
import random
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

from app.tasks import runtime
from app.tasks.runtime import is_blank, is_num

_ASCII_TO_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
_HEH_TO_TEH_MARBUTA = str.maketrans({"ه": "ة"})
_YEH_TO_ALEF_MAQSURA = str.maketrans({"ي": "ى"})


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _arabic_digit_variant(value: Any) -> Any:
    """ASCII digits → Arabic-Indic; no-op when there are none to swap."""
    if not isinstance(value, str) or not any(ch in value for ch in "0123456789"):
        return value
    return value.translate(_ASCII_TO_ARABIC_DIGITS)


def _arabic_name_variant(value: Any) -> Any:
    """A different-looking Arabic spelling that arabic_text should still fold."""
    if not isinstance(value, str) or not value:
        return value
    s = value
    if "ا" in s:
        s = s.replace("ا", "أ", 1)
    s = s.translate(_YEH_TO_ALEF_MAQSURA).translate(_HEH_TO_TEH_MARBUTA)
    if s and "\u0640" not in s:
        # Insert tatweel after the first Arabic letter when present.
        for i, ch in enumerate(s):
            if "\u0600" <= ch <= "\u06FF":
                s = s[:i + 1] + "\u0640" + s[i + 1:]
                break
    if s == value and any("\u0600" <= ch <= "\u06FF" for ch in s):
        s = s + "\u064E"   # fatha diacritic — stripped by arabic_text
    return s


def _arabic_separator_variant(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if "." not in value and "," not in value:
        return value
    return value.replace(".", "٫").replace(",", "٬")


# Latin given-name spelling swaps QA uses when arabic_names is on the key.
_LATIN_NAME_SWAPS = (
    ("Mohammed", "Muhammad"), ("Muhammad", "Mohamed"), ("Mohamed", "Mohd"),
    ("Mohd", "Mohammed"), ("Ahmed", "Ahmad"), ("Ahmad", "Ahmed"),
    ("Khaled", "Khalid"), ("Khalid", "Khaled"), ("Hassan", "Hasan"),
)


def _latin_name_variant(value: Any) -> Any:
    """A different Latin spelling that arabic_names should still fold to the same key."""
    if not isinstance(value, str) or not value:
        return value
    for src, dst in _LATIN_NAME_SWAPS:
        if src in value:
            return value.replace(src, dst, 1)
        if src.lower() in value:
            return value.replace(src.lower(), dst.lower(), 1)
        if src.upper() in value:
            return value.replace(src.upper(), dst.upper(), 1)
    return value


_RUNTIME_SOURCE = Path(runtime.__file__).read_text(encoding="utf-8")


def compile_spec(spec: dict) -> str:
    """A standalone module implementing the spec with the reference runtime (sample mode)."""
    body = re.sub(r"^from __future__ import annotations\n", "", _RUNTIME_SOURCE, flags=re.M)
    body = re.sub(r'^"""Reference semantics.*?"""\n', "", body, count=1, flags=re.S)
    return (f'"""{spec["name"]}: {spec["summary"]}\n\nCompiled by AgentOffice from the approved spec (sample '
            f'mode): the step runtime is embedded below.\n"""\n\nimport json\n{body}\n\n'
            f"SPEC = json.loads({json.dumps(json.dumps(spec))})\n\n\n"
            "def run(rows, reference):\n    return execute(SPEC, rows, reference)\n")


def expected(spec: dict, rows: list[dict], reference: dict) -> dict:
    """{"ok": True, "output": ...} or {"ok": False, "error": "ValueError: ..."} from the oracle."""
    try:
        return {"ok": True, "output": runtime.execute(spec, rows, reference)}
    except ValueError as e:
        return {"ok": False, "error": f"ValueError: {e}"}


def _variant(v: Any) -> Any:
    return f"  {v.swapcase()}  " if isinstance(v, str) else v


def _first(rows: list[dict], col: str) -> Any:
    return next((r.get(col) for r in rows if not is_blank(r.get(col))), None)


def build_cases(spec: dict, rows: list[dict], reference: dict, *, seed: int = 0, sample_size: int = 25,
                full_file: bool = True) -> list[dict]:
    """[{"purpose", "kind", "rows", "reference"}]; kinds: sample, full, mutation, edge."""
    rng = random.Random(seed)
    declared = spec["input"]["columns"]
    base_rows = rows or [{c: None for c in declared}]
    cases: list[dict] = []

    def pick() -> dict:
        return copy.deepcopy(rng.choice(base_rows))

    def add(purpose: str, kind: str, case_rows: list[dict], ref: Optional[dict] = None) -> None:
        cases.append({"purpose": purpose, "kind": kind, "rows": case_rows,
                      "reference": reference if ref is None else ref})

    head = rows[:12]
    rest = rows[12:]
    sample = head + rng.sample(rest, min(len(rest), max(0, sample_size - len(head))))
    add(f"{len(sample)} real rows from your file", "sample", copy.deepcopy(sample))

    # English date steps skip the Arabic-digit mutation, so they never have to handle Indic digits to pass.
    english_dates = {s["column"] for s in spec["steps"] if s["op"] == "format_date" and not s.get("arabic")}
    for col in declared:
        v = _first(rows, col)
        variants: list[tuple[str, Any]] = [("blank", None)]
        if isinstance(v, str):
            variants += [("extra whitespace", f"   {v}   "), ("different case", v.swapcase())]
            digit_v = _arabic_digit_variant(v)
            if digit_v != v and col not in english_dates:
                variants.append(("Arabic-Indic digits", digit_v))
            name_v = _arabic_name_variant(v)
            if name_v != v:
                variants.append(("Arabic name spelling variant", name_v))
            latin_v = _latin_name_variant(v)
            if latin_v != v:
                variants.append(("Latin name spelling variant", latin_v))
            sep_v = _arabic_separator_variant(v)
            if sep_v != v:
                variants.append(("Arabic decimal/thousands separators", sep_v))
        if is_num(v):
            variants += [("zero", 0), ("negative", -abs(v) or -1), ("number typed as text", f"{v}")]
            as_text = _arabic_digit_variant(f"{v}")
            if as_text != f"{v}":
                variants.append(("number as Arabic-Indic digits", as_text))
        for why, value in variants:
            r = pick()
            r[col] = value
            add(f"{col}: {why}", "mutation", [r])

    for i, step in enumerate(spec["steps"]):
        label = f"step {i + 1} ({step['op']})"
        for purpose, case_rows, ref in _edge_cases(step, rows, reference, pick):
            add(f"{label}: {purpose}", "edge", case_rows, ref)

    if full_file and len(rows) > len(sample):
        add(f"your whole file ({len(rows)} rows)", "full", copy.deepcopy(rows))
    return cases


def _edge_cases(step: dict, rows: list[dict], reference: dict, pick) -> list[tuple[str, list[dict], Optional[dict]]]:
    op = step["op"]
    out: list[tuple[str, list[dict], Optional[dict]]] = []
    if op == "lookup":
        ref_rows = reference.get(step["reference"], [])
        key_field = step["reference_key"]
        steps = step.get("normalize", [])
        known = next((r for r in ref_rows if not is_blank(r.get(key_field))), None)
        if known is not None:
            r = pick()
            r[step["key"]] = _variant(known[key_field]) if isinstance(known[key_field], str) else known[key_field]
            out.append(("key differs only in case and spaces", [r], None))
            raw = known[key_field]
            if "arabic_digits" in steps or "digits_only" in steps:
                digit_v = _arabic_digit_variant(raw if isinstance(raw, str) else _as_text(raw))
                if digit_v != raw:
                    r = pick()
                    r[step["key"]] = digit_v
                    out.append(("key uses Arabic-Indic digits", [r], None))
            if "arabic_text" in steps and isinstance(raw, str):
                name_v = _arabic_name_variant(raw)
                if name_v != raw:
                    r = pick()
                    r[step["key"]] = name_v
                    out.append(("key uses an Arabic spelling variant", [r], None))
            if "arabic_names" in steps and isinstance(raw, str):
                latin_v = _latin_name_variant(raw)
                if latin_v != raw:
                    r = pick()
                    r[step["key"]] = latin_v
                    out.append(("key uses a Latin name spelling variant", [r], None))
                for src, ar in (("Mohammed", "محمد"), ("Muhammad", "محمد"), ("Ahmed", "أحمد"), ("Ahmad", "أحمد")):
                    if src in raw:
                        r = pick()
                        r[step["key"]] = raw.replace(src, ar, 1)
                        out.append(("key uses Arabic script for a Latin directory name", [r], None))
                        break
            if "arabic_separators" in steps and isinstance(raw, str):
                sep_v = _arabic_separator_variant(raw)
                if sep_v != raw:
                    r = pick()
                    r[step["key"]] = sep_v
                    out.append(("key uses Arabic decimal/thousands separators", [r], None))
            r = pick()
            r[step["key"]] = known[key_field]
            for col in step["fields"]:
                r[col] = "Existing value"
            out.append(("output column already has a value", [r], None))
            dup = copy.deepcopy(known)
            for field in step["fields"].values():
                if isinstance(dup.get(field), str):
                    dup[field] = dup[field] + " (second record)"
            ref = {**reference, step["reference"]: ref_rows + [dup]}
            r = pick()
            r[step["key"]] = known[key_field]
            out.append(("two reference records share the key", [r], ref))
        r = pick()
        r[step["key"]] = "zz-no-such-key@example.invalid"
        out.append(("key not in the reference data", [r], None))
        r = pick()
        r[step["key"]] = "   "
        out.append(("key is only spaces", [r], None))
    elif op == "compute":
        for col in runtime.expression_columns(step["expression"]):
            r = pick()
            r[col] = "12.5"
            out.append((f"{col} is a number typed as text", [r], None))
            r = pick()
            r[col] = 0
            out.append((f"{col} is zero", [r], None))
    elif op in ("flag", "filter"):
        conds = step["when"] if op == "flag" else step["keep_when"]
        for c in conds:
            value = c.get("value")
            if is_num(value):
                for delta, why in ((0, "equals"), (0.01, "just above"), (-0.01, "just below")):
                    r = pick()
                    r[c["column"]] = value + delta
                    out.append((f"{c['column']} {why} {value}", [r], None))
            elif isinstance(value, str):
                r = pick()
                r[c["column"]] = _variant(value)
                out.append((f"{c['column']} is {value!r} with other case and spaces", [r], None))
                r = pick()
                r[c["column"]] = value
                out.append((f"{c['column']} is exactly {value!r}", [r], None))
    elif op == "format_date":
        day = date(2026, 3, 7)
        for fmt in step["input_formats"]:
            r = pick()
            r[step["column"]] = day.strftime(fmt)
            out.append((f"date written as {fmt}", [r], None))
        # Arabic month names and Hijri dates are tested only on a step that opts in with "arabic": true, so an
        # English date task never depends on reproducing the Hijri calendar.
        if step.get("arabic"):
            r = pick()
            r[step["column"]] = "١٥ مارس ٢٠٢٦"
            out.append(("date with Arabic month name and Indic digits", [r], None))
            r = pick()
            r[step["column"]] = "15 رمضان 1447"
            out.append(("Hijri date", [r], None))
        for bad, why in (("not a date", "text that is not a date"), (46000, "an Excel serial number")):
            r = pick()
            r[step["column"]] = bad
            out.append((f"{why}", [r], None))
    elif op == "parse_amount":
        for sample, why in (("د.إ ١٬٢٣٤٫٥٠", "AED Arabic digits and separators"),
                            ("ر.س 100.00", "Saudi riyal prefix"),
                            ("AED 1,234.50", "Latin AED prefix")):
            r = pick()
            r[step["column"]] = sample
            out.append((f"amount with {why}", [r], None))
    elif op == "dedupe":
        a = pick()
        b = copy.deepcopy(a)
        for col in step["columns"]:
            b[col] = _variant(b.get(col))
        blank_a, blank_b = pick(), pick()
        for col in step["columns"]:
            blank_a[col] = blank_b[col] = None
        out.append(("the same key typed differently", [a, b], None))
        out.append(("two rows with an empty key", [blank_a, blank_b], None))
    elif op == "sort":
        col = step["by"][0]["column"]
        mixed = [pick() for _ in range(5)]
        for r, v in zip(mixed, [None, "b", 3, "A", 1.5]):
            r[col] = v
        out.append((f"{col} mixes blanks, text and numbers", mixed, None))
    elif op == "split":
        for value, why in (("Alpha  Beta Gamma Delta", "more parts than columns"), ("Solo", "only one part"),
                           ("   ", "only spaces")):
            r = pick()
            r[step["column"]] = value
            out.append((why, [r], None))
    elif op == "map_values":
        key = next(iter(step["mapping"]))
        r = pick()
        r[step["column"]] = _variant(key)
        out.append(("value differs from a mapping key only in case and spaces", [r], None))
    elif op == "fill_blank":
        r = pick()
        r[step["column"]] = "   "
        out.append(("value is only spaces", [r], None))
    elif op == "clean_text":
        r = pick()
        r[step["columns"][0]] = "  several   inner    spaces  "
        out.append(("runs of inner spaces", [r], None))
        r = pick()
        r[step["columns"][0]] = 42
        out.append(("a number in a text column", [r], None))
    elif op == "summarize":
        group = [pick() for _ in range(3)]
        group[0][step["group_by"][0]] = None
        group[1][step["values"][0]["column"]] = "n/a"
        out.append(("blank group and a non-number value", group, None))
    elif op == "concat":
        r = pick()
        for col in re.findall(r"\{([^{}]+)\}", step["template"]):
            r[col] = None
        out.append(("every part blank", [r], None))
    return out


def drift_case(spec: dict, rows: list[dict], reference: dict) -> dict:
    """The sample with the first declared input column renamed: correct code must raise."""
    col = spec["input"]["columns"][0]
    renamed = [{(f"{col} (renamed)" if k == col else k): v for k, v in r.items()} for r in rows[:5]]
    return {"purpose": f"column {col!r} renamed", "kind": "drift", "rows": renamed, "reference": reference,
            "renamed": col}


def _same(a: Any, b: Any) -> bool:
    if is_blank(a) and is_blank(b):   # None, '' and spaces all write an empty cell
        return True
    if is_num(a) and is_num(b):
        return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))
    return type(a) is type(b) and a == b


def _diff_rows(exp_rows: list[dict], got_rows: Any, what: str) -> Optional[str]:
    if not isinstance(got_rows, list):
        return f"{what} is not a list"
    if len(got_rows) != len(exp_rows):
        return f"{what}: expected {len(exp_rows)} rows, got {len(got_rows)}"
    for i, (e, g) in enumerate(zip(exp_rows, got_rows)):
        if not isinstance(g, dict):
            return f"{what}[{i}] is not an object"
        if set(g) != set(e):
            missing, extra = sorted(set(e) - set(g)), sorted(set(g) - set(e))
            return f"{what}[{i}] columns differ (missing {missing}, unexpected {extra})"
        for col in e:
            if not _same(e[col], g[col]):
                return f"{what}[{i}][{col!r}]: expected {json.dumps(e[col])}, got {json.dumps(g[col])}"
    return None


def compare(exp: dict, got: dict) -> Optional[str]:
    """None when the code's result matches the oracle's, else the first difference in words."""
    if not exp["ok"]:
        if got.get("ok"):
            return f"expected {exp['error'].split(':')[0]}, but the code returned a result"
        return None if str(got.get("error", "")).startswith("ValueError") else \
            f"expected ValueError, got {got.get('error')}"
    if not got.get("ok"):
        return f"raised {got.get('error')}"
    output = got["output"]
    if not isinstance(output, dict) or "rows" not in output:
        return "run() must return {\"rows\": [...], \"sheets\": {...}}"
    problem = _diff_rows(exp["output"]["rows"], output["rows"], "rows")
    if problem:
        return problem
    sheets = output.get("sheets") or {}
    if not isinstance(sheets, dict) or set(sheets) != set(exp["output"]["sheets"]):
        return f"sheets: expected {sorted(exp['output']['sheets'])}, got {sorted(sheets) if isinstance(sheets, dict) else sheets}"
    for name, records in exp["output"]["sheets"].items():
        problem = _diff_rows(records, sheets[name], f"sheets[{name!r}]")
        if problem:
            return problem
    return None
