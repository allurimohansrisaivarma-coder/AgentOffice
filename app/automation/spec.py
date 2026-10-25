"""The automation spec: a structured plan, not free text.

Every automation AgentOffice builds is a record matcher/classifier:

    for each LEFT record (e.g. an invoice), find the RIGHT record (e.g. a
    purchase order) whose normalized key equals its normalized key, run the
    checks in order, and emit {"left_id", "right_id", "status"}.

That shape covers the routines in the data (invoice -> PO matching, lead ->
CRM contact dedupe, status request -> order lookup) and, crucially, has exact
semantics, so QA can compute expected outputs from the spec alone.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

OPS = {
    "equals": "string values equal after stripping surrounding whitespace",
    "equals_ci": "string values equal after stripping whitespace and ignoring case",
    "within_pct": "numbers satisfy abs(left - right) <= value / 100 * abs(right)",
    "within_abs": "numbers satisfy abs(left - right) <= value",
}
NORMALIZERS = {
    "strip": "remove surrounding whitespace",
    "upper": "uppercase",
    "lower": "lowercase",
    "collapse_spaces": "replace runs of whitespace with one space",
    "digits_only": "keep only the digits 0-9",
}
FIELD_TYPES = ("str", "number")
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_spec(spec: Any) -> list[str]:
    """Every problem with a spec, as messages a model can act on. Empty means valid."""
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]
    errors: list[str] = []
    for key in ("name", "routine", "summary", "left", "right", "key", "checks", "statuses"):
        if key not in spec:
            errors.append(f"missing '{key}'")
    if errors:
        return errors
    if not _IDENT.match(str(spec["name"])):
        errors.append("name must be snake_case (a-z, 0-9, _), max 41 chars")

    sides = {}
    for side in ("left", "right"):
        s = spec[side]
        if not isinstance(s, dict) or not isinstance(s.get("fields"), dict) or not s.get("fields"):
            errors.append(f"{side} must have 'name', 'id_field' and a non-empty 'fields' map of name -> type")
            continue
        for fname, ftype in s["fields"].items():
            if not _IDENT.match(fname):
                errors.append(f"{side} field {fname!r} must be snake_case")
            if ftype not in FIELD_TYPES:
                errors.append(f"{side} field {fname!r} has type {ftype!r}; use one of {FIELD_TYPES}")
        if s.get("id_field") not in s["fields"]:
            errors.append(f"{side}.id_field must be one of its fields")
        if not _IDENT.match(str(s.get("name", ""))):
            errors.append(f"{side}.name must be snake_case")
        sides[side] = s["fields"]
    if len(sides) < 2:
        return errors

    key = spec["key"]
    if not isinstance(key, dict):
        errors.append("key must be an object with 'left', 'right' and 'normalize'")
    else:
        if key.get("left") not in sides["left"] or key.get("right") not in sides["right"]:
            errors.append("key.left / key.right must name fields of left / right")
        elif sides["left"][key["left"]] != "str" or sides["right"][key["right"]] != "str":
            errors.append("key fields must be of type str")
        bad = [n for n in key.get("normalize", []) if n not in NORMALIZERS]
        if not isinstance(key.get("normalize", []), list) or bad:
            errors.append(f"key.normalize must be a list drawn from {sorted(NORMALIZERS)}")

    checks = spec["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 5:
        errors.append("checks must be a list of 1 to 5 checks")
        checks = []
    statuses = spec["statuses"]
    if not isinstance(statuses, dict) or set(statuses) != {"no_match", "ambiguous", "pass"}:
        errors.append("statuses must have exactly 'no_match', 'ambiguous' and 'pass'")
        statuses = {}
    all_statuses = list(statuses.values())
    for i, c in enumerate(checks):
        where = f"checks[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{where} must be an object")
            continue
        if c.get("op") not in OPS:
            errors.append(f"{where}.op must be one of {sorted(OPS)}")
            continue
        lf, rf = c.get("left"), c.get("right")
        if lf not in sides["left"] or rf not in sides["right"]:
            errors.append(f"{where}.left/right must name fields of left/right")
            continue
        wanted = "number" if c["op"].startswith("within") else "str"
        if sides["left"][lf] != wanted or sides["right"][rf] != wanted:
            errors.append(f"{where}: op {c['op']} needs {wanted} fields on both sides")
        if c["op"].startswith("within") and (not _is_number(c.get("value")) or c["value"] < 0):
            errors.append(f"{where}.value must be a number >= 0")
        all_statuses.append(c.get("fail_status"))
    for s in all_statuses:
        if not isinstance(s, str) or not _IDENT.match(s):
            errors.append(f"status {s!r} must be snake_case")
    if len(set(all_statuses)) != len(all_statuses):
        errors.append("every status (no_match, ambiguous, pass and each check's fail_status) must be distinct")
    return errors


def normalize_key(value: Any, steps: list[str]) -> str:
    text = "" if value is None else str(value)
    for step in steps:
        if step == "strip":
            text = text.strip()
        elif step == "upper":
            text = text.upper()
        elif step == "lower":
            text = text.lower()
        elif step == "collapse_spaces":
            text = " ".join(text.split())
        elif step == "digits_only":
            text = "".join(ch for ch in text if ch in "0123456789")
    return text


def check_passes(check: dict, left_value: Any, right_value: Any) -> bool:
    op = check["op"]
    if op in ("equals", "equals_ci"):
        if not isinstance(left_value, str) or not isinstance(right_value, str):
            return False
        a, b = left_value.strip(), right_value.strip()
        return a == b if op == "equals" else a.casefold() == b.casefold()
    if not _is_number(left_value) or not _is_number(right_value):
        return False
    limit = check["value"] / 100 * abs(right_value) if op == "within_pct" else check["value"]
    return abs(left_value - right_value) <= limit


def describe(spec: dict) -> str:
    """The exact semantics, in words, for the Engineer's prompt."""
    left, right, key = spec["left"], spec["right"], spec["key"]
    steps = ", then ".join(f"{n} ({NORMALIZERS[n]})" for n in key.get("normalize", [])) or "none"
    lines = [
        f"Automation `{spec['name']}`: {spec['summary']}",
        f"Input: left = list of `{left['name']}` records with fields {json.dumps(left['fields'])}; "
        f"right = list of `{right['name']}` records with fields {json.dumps(right['fields'])}.",
        "If any record lacks one of its declared fields, raise ValueError naming the field "
        "(never guess or skip: a renamed column must fail loudly).",
        f"For each left record, in input order: compute k = left['{key['left']}'] normalized by: {steps}.",
        f"  - If k is empty, or no right record has right['{key['right']}'] normalized the same way equal to k: "
        f"status '{spec['statuses']['no_match']}', right_id null.",
        f"  - If more than one right record matches: status '{spec['statuses']['ambiguous']}', right_id null.",
        f"  - Otherwise take the single match r (right_id = r['{right['id_field']}']) and run the checks in order; "
        "the first failing check decides the status:",
    ]
    for c in spec["checks"]:
        value = f" with value {c['value']}" if "value" in c else ""
        lines.append(f"      {c['op']}(left['{c['left']}'], right['{c['right']}']){value}: {OPS[c['op']]}; "
                     f"fails -> '{c['fail_status']}'. A value of the wrong type fails the check.")
    lines += [
        f"  - If every check passes: status '{spec['statuses']['pass']}'.",
        f"Output: a list with one object per left record, same order: "
        f"{{\"left_id\": left['{left['id_field']}'], \"right_id\": <str or null>, \"status\": <str>}}.",
    ]
    return "\n".join(lines)


TEMPLATES: dict[str, dict] = {
    "invoice_matching": {
        "name": "invoice_po_matcher", "routine": "invoice_matching",
        "summary": "Match each vendor invoice to its purchase order and flag vendor or amount mismatches.",
        "left": {"name": "invoices", "id_field": "invoice_id",
                 "fields": {"invoice_id": "str", "vendor": "str", "po_number": "str", "amount": "number"}},
        "right": {"name": "purchase_orders", "id_field": "po_number",
                  "fields": {"po_number": "str", "vendor": "str", "amount": "number"}},
        "key": {"left": "po_number", "right": "po_number", "normalize": ["strip", "upper"]},
        "checks": [
            {"left": "vendor", "right": "vendor", "op": "equals_ci", "fail_status": "vendor_mismatch"},
            {"left": "amount", "right": "amount", "op": "within_pct", "value": 1.0, "fail_status": "amount_mismatch"},
        ],
        "statuses": {"no_match": "no_po", "ambiguous": "ambiguous_po", "pass": "matched"},
    },
    "crm_data_entry": {
        "name": "lead_contact_deduper", "routine": "crm_data_entry",
        "summary": "Decide for each inbound lead whether it is a new contact, a duplicate, or needs an update.",
        "left": {"name": "leads", "id_field": "lead_id",
                 "fields": {"lead_id": "str", "email": "str", "company": "str", "phone": "str"}},
        "right": {"name": "contacts", "id_field": "contact_id",
                  "fields": {"contact_id": "str", "email": "str", "company": "str", "phone": "str"}},
        "key": {"left": "email", "right": "email", "normalize": ["strip", "lower"]},
        "checks": [
            {"left": "company", "right": "company", "op": "equals_ci", "fail_status": "update_company"},
            {"left": "phone", "right": "phone", "op": "equals", "fail_status": "update_phone"},
        ],
        "statuses": {"no_match": "create_contact", "ambiguous": "manual_review", "pass": "duplicate"},
    },
    "order_status_updates": {
        "name": "order_status_responder", "routine": "order_status_updates",
        "summary": "Look up the order behind each customer status request and confirm it belongs to that customer.",
        "left": {"name": "status_requests", "id_field": "request_id",
                 "fields": {"request_id": "str", "order_ref": "str", "customer": "str"}},
        "right": {"name": "orders", "id_field": "order_id",
                  "fields": {"order_id": "str", "customer": "str", "status": "str"}},
        "key": {"left": "order_ref", "right": "order_id", "normalize": ["digits_only"]},
        "checks": [
            {"left": "customer", "right": "customer", "op": "equals_ci", "fail_status": "customer_mismatch"},
        ],
        "statuses": {"no_match": "order_not_found", "ambiguous": "manual_review", "pass": "reply_with_status"},
    },
}


def template_for(routine: str) -> dict:
    return copy.deepcopy(TEMPLATES[routine])
