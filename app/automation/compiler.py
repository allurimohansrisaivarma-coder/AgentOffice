"""Offline Engineer fallback: compile a spec into a standalone Python module.

Used only when no model is available (sample mode), and logged as
`offline-fallback`. QA runs exactly the same battery against this code as
against model-written code.
"""

from __future__ import annotations

import json

from app.automation.spec import NORMALIZERS

_NORMALIZE_CODE = {
    "strip": "text = text.strip()",
    "upper": "text = text.upper()",
    "lower": "text = text.lower()",
    "collapse_spaces": "text = ' '.join(text.split())",
    "digits_only": (
        "text = ''.join(ch for ch in text.translate("
        "str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')) if ch in '0123456789')"
    ),
    "arabic_digits": "text = text.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789'))",
    "arabic_text": (
        "_marks = set('\\u0640' + ''.join(chr(c) for c in range(0x064B, 0x0660)) + '\\u0670'); "
        "text = ''.join(ch for ch in text.translate(str.maketrans({"
        "'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ٱ': 'ا', 'ى': 'ي', 'ة': 'ه'})) if ch not in _marks)"
    ),
    "arabic_separators": "text = text.translate(str.maketrans({'٫': '.', '٬': ','}))",
}

_CHECK_CODE = {
    "equals": "isinstance(a, str) and isinstance(b, str) and a.strip() == b.strip()",
    "equals_ci": "isinstance(a, str) and isinstance(b, str) and a.strip().casefold() == b.strip().casefold()",
    "within_pct": "_num(a) and _num(b) and abs(a - b) <= {value!r} / 100 * abs(b)",
    "within_abs": "_num(a) and _num(b) and abs(a - b) <= {value!r}",
}


def compile_spec(spec: dict) -> str:
    key, statuses = spec["key"], spec["statuses"]
    normalize = "\n".join(f"    {_NORMALIZE_CODE[n]}" for n in key.get("normalize", []) if n in NORMALIZERS)
    check_bindings = "\n".join(
        f"    a, b = left[{c['left']!r}], right[{c['right']!r}]\n"
        f"    if not ({_CHECK_CODE[c['op']].format(value=c.get('value'))}):\n"
        f"        return {c['fail_status']!r}"
        for c in spec["checks"])
    return f'''"""{spec['name']}: {spec['summary']}

Compiled offline from the automation spec (no model available).
"""

LEFT_FIELDS = {json.dumps(list(spec['left']['fields']))}
RIGHT_FIELDS = {json.dumps(list(spec['right']['fields']))}


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require(record, fields, side):
    for name in fields:
        if name not in record:
            raise ValueError(side + " record is missing field " + repr(name))


def _key(value):
    text = "" if value is None else str(value)
{normalize or "    pass"}
    return text


def _status(left, right):
{check_bindings}
    return {statuses['pass']!r}


def run(left, right):
    for record in right:
        _require(record, RIGHT_FIELDS, "right")
    for record in left:
        _require(record, LEFT_FIELDS, "left")
    index = {{}}
    for record in right:
        index.setdefault(_key(record[{key['right']!r}]), []).append(record)
    results = []
    for record in left:
        k = _key(record[{key['left']!r}])
        matches = index.get(k, []) if k else []
        row = {{"left_id": record[{spec['left']['id_field']!r}], "right_id": None, "status": {statuses['no_match']!r}}}
        if len(matches) > 1:
            row["status"] = {statuses['ambiguous']!r}
        elif len(matches) == 1:
            row["right_id"] = matches[0][{spec['right']['id_field']!r}]
            row["status"] = _status(record, matches[0])
        results.append(row)
    return results
'''
