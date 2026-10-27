"""Reference semantics of a spreadsheet task spec: QA's oracle.

A task spec (app/tasks/spec.py) is an ordered list of steps over a table of
rows. This module is the exact meaning of every step. QA computes expected
output with it and compares the Engineer's code against that, so the tests
never come from the code under test. Sample mode also compiles a spec into a
standalone module by embedding this file, so it must stay self-contained:
standard library only, no imports from the app.

Values are str, int, float or None. A value is blank when it is None or a
string that is empty after stripping whitespace.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime

NORMALIZERS = (
    "strip", "upper", "lower", "collapse_spaces", "digits_only",
    "arabic_digits", "arabic_text", "arabic_separators", "arabic_names",
)

# Arabic-Indic (U+0660..) and Extended Arabic-Indic / Persian (U+06F0..) -> ASCII.
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# Alef variants, alef maqsura/yeh, teh marbuta/heh — common UAE/Saudi name spellings.
_ARABIC_LETTERS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه"})
# Arabic decimal and thousands separators (U+066B, U+066C).
_ARABIC_SEPARATORS = str.maketrans({"٫": ".", "٬": ","})
# Tatweel + harakat / superscript alef (not digits: those start at U+0660).
_ARABIC_MARKS = frozenset("\u0640" + "".join(chr(c) for c in range(0x064B, 0x0660)) + "\u0670")

# Cross-script given-name folding: محمد ↔ Mohammed/Muhammad/Mohamed/Mohd, etc.
# Keys on the Arabic side are post-arabic_text forms; Latin aliases map to the same canon.
# Keys are post-arabic_text forms (ة→ه, ى→ي, أ/إ/آ→ا) so lookup after folding works.
_NAME_AR_TO_CANON = {
    "محمد": "mohammad", "محمود": "mahmoud", "احمد": "ahmad", "علي": "ali", "حسن": "hassan",
    "حسين": "hussein", "خالد": "khalid", "عمر": "omar", "عثمان": "othman", "يوسف": "youssef",
    "ابراهيم": "ibrahim", "اسماعيل": "ismail", "عبدالله": "abdullah", "عبدالرحمن": "abdulrahman",
    "عبدالعزيز": "abdulaziz", "فهد": "fahad", "سلطان": "sultan", "سعود": "saud", "ناصر": "nasser",
    "سالم": "salem", "سعيد": "saeed", "راشد": "rashid", "حمد": "hamad", "حمدان": "hamdan",
    "فاطمه": "fatima", "عائشه": "aisha", "مريم": "maryam", "نوره": "noura", "هند": "hind",
    "ساره": "sara", "لينا": "lina", "ريم": "reem", "مني": "mona", "ندي": "nada",
}
_NAME_LATIN_TO_CANON = {}
for _canon, _aliases in {
    "mohammad": ("mohammad", "muhammad", "mohammed", "mohamed", "mohd", "muhamed", "muhammed"),
    "mahmoud": ("mahmoud", "mahmud", "mahmood"),
    "ahmad": ("ahmad", "ahmed"),
    "ali": ("ali",),
    "hassan": ("hassan", "hasan"),
    "hussein": ("hussein", "hussain", "hosein"),
    "khalid": ("khalid", "khaled"),
    "omar": ("omar", "umar"),
    "othman": ("othman", "uthman", "osman"),
    "youssef": ("youssef", "yousef", "yusuf", "yosef"),
    "ibrahim": ("ibrahim", "ebraheem", "ibraheem"),
    "ismail": ("ismail", "ismael"),
    "abdullah": ("abdullah", "abdalla", "abdallah", "abdollah"),
    "abdulrahman": ("abdulrahman", "abdurrahman", "abdul-rahman"),
    "abdulaziz": ("abdulaziz", "abdul-aziz", "abdelaziz"),
    "fahad": ("fahad", "fahd"),
    "sultan": ("sultan",),
    "saud": ("saud",),
    "nasser": ("nasser", "nasir"),
    "salem": ("salem", "salim"),
    "saeed": ("saeed", "said", "saeid"),
    "rashid": ("rashid", "rashed"),
    "hamad": ("hamad", "hamed", "hamid"),
    "hamdan": ("hamdan",),
    "fatima": ("fatima", "fatimah", "fatma"),
    "aisha": ("aisha", "aysha", "aishah"),
    "maryam": ("maryam", "mariam"),
    "noura": ("noura", "nora", "norah"),
    "hind": ("hind",),
    "sara": ("sara", "sarah"),
    "lina": ("lina", "leena"),
    "reem": ("reem", "rim"),
    "mona": ("mona", "muna"),
    "nada": ("nada",),
}.items():
    for _a in _aliases:
        _NAME_LATIN_TO_CANON[_a] = _canon

# Gregorian month names in Arabic → English abbreviation for strptime %b.
_AR_GREGORIAN_MONTHS = {
    "يناير": "Jan", "فبراير": "Feb", "مارس": "Mar", "أبريل": "Apr", "ابريل": "Apr",
    "مايو": "May", "يونيو": "Jun", "يونية": "Jun", "يوليو": "Jul", "يولية": "Jul",
    "أغسطس": "Aug", "اغسطس": "Aug", "سبتمبر": "Sep", "أكتوبر": "Oct", "اكتوبر": "Oct",
    "نوفمبر": "Nov", "ديسمبر": "Dec",
}
# Hijri month name → month number (1-12).
_HIJRI_MONTHS = {
    "محرم": 1, "صفر": 2, "ربيع الأول": 3, "ربيع اول": 3, "ربيع الآخر": 4, "ربيع الثاني": 4,
    "جمادى الأولى": 5, "جمادى الاولى": 5, "جمادى الأول": 5, "جمادى الآخرة": 6, "جمادى الاخرة": 6,
    "جمادى الثانية": 6, "رجب": 7, "شعبان": 8, "رمضان": 9, "شوال": 10,
    "ذو القعدة": 11, "ذي القعدة": 11, "ذو الحجة": 12, "ذي الحجة": 12,
}
_CURRENCY_TOKENS = (
    "د.إ.", "د.إ", "دإ", "ر.س.", "ر.س", "رس", "AED", "SAR", "USD", "GBP", "EUR",
    "DH", "Dhs", "د.ا.إ", "درهم", "ريال", "فلس",
)
_AMOUNT_NOISE = re.compile(r"[^\d.\-]+")
_HIJRI_RE = re.compile(
    r"^(\d{1,2})\s+(" + "|".join(re.escape(m) for m in sorted(_HIJRI_MONTHS, key=len, reverse=True))
    + r")\s+(\d{3,4})$"
)


class SpecError(ValueError):
    pass


def is_blank(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def text(v):
    """A value as text: None -> "", whole floats without ".0", everything else str()."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def arabic_digits(s):
    return s.translate(_ARABIC_DIGITS)


def arabic_text(s):
    return "".join(ch for ch in s.translate(_ARABIC_LETTERS) if ch not in _ARABIC_MARKS)


def arabic_separators(s):
    return s.translate(_ARABIC_SEPARATORS)


def _name_token_canon(token: str) -> str:
    """Fold one name token (already arabic_text + lower) to a Latin canon when known."""
    if not token:
        return token
    if token in _NAME_AR_TO_CANON:
        return _NAME_AR_TO_CANON[token]
    latin = _NAME_LATIN_TO_CANON.get(token)
    if latin:
        return latin
    # Drop Arabic script leftovers that we don't map (keep Latin surname as-is).
    if any("\u0600" <= ch <= "\u06FF" for ch in token):
        return token
    return token


def arabic_names(s):
    """Transliteration-aware name key: محمد علي and Mohammed Ali → 'mohammad ali'."""
    folded = arabic_text(arabic_digits(s))
    tokens = " ".join(folded.split()).casefold().split()
    return " ".join(_name_token_canon(t) for t in tokens)


def parse_amount(v):
    """Number from Gulf amount strings (د.إ ١٬٢٣٤٫٥٠ / AED 1,234.50) or a real int/float."""
    if is_num(v):
        return float(v)
    if not isinstance(v, str) or not v.strip():
        return None
    s = arabic_digits(arabic_separators(v.strip()))
    for tok in _CURRENCY_TOKENS:
        s = s.replace(tok, "")
        s = s.replace(tok.lower(), "")
        s = s.replace(tok.upper(), "")
    s = s.replace(",", "").strip()
    s = _AMOUNT_NOISE.sub("", s)
    if not s or s in {".", "-", "-."}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _hijri_to_gregorian(y: int, m: int, d: int):
    """Kuwaiti algorithm: Islamic civil calendar → Gregorian date."""
    # Source: widely used civil Hijri conversion (Fliegel / Kuwaiti).
    jd = int((11 * y + 3) / 30) + 354 * y + 30 * m - int((m - 1) / 2) + d + 1948440 - 385
    l = jd + 68569
    n = int((4 * l) / 146097)
    l = l - int((146097 * n + 3) / 4)
    i = int((4000 * (l + 1)) / 1461001)
    l = l - int((1461 * i) / 4) + 31
    j = int((80 * l) / 2447)
    day = l - int((2447 * j) / 80)
    l = int(j / 11)
    month = j + 2 - 12 * l
    year = 100 * (n - 49) + i + l
    return datetime(year, month, day)


def prepare_date_string(s: str) -> str:
    """Arabic-Indic digits + Arabic Gregorian month names → strptime-friendly text."""
    s = arabic_digits(s.strip())
    # Longer month phrases first is handled by iterating sorted-by-length.
    for ar, en in sorted(_AR_GREGORIAN_MONTHS.items(), key=lambda kv: -len(kv[0])):
        if ar in s:
            s = s.replace(ar, en)
    return " ".join(s.split())


def parse_date_value(s: str, input_formats):
    """Parse a date string with Arabic months / Hijri / listed strptime formats. None on miss."""
    if not isinstance(s, str) or not s.strip():
        return None
    prepared = prepare_date_string(s)
    hijri = _HIJRI_RE.match(prepared)
    if hijri:
        day_i, month_name, year_i = int(hijri.group(1)), hijri.group(2), int(hijri.group(3))
        month_i = _HIJRI_MONTHS.get(month_name)
        if month_i:
            try:
                return _hijri_to_gregorian(year_i, month_i, day_i)
            except ValueError:
                pass
    for fmt in input_formats:
        try:
            return datetime.strptime(prepared, fmt)
        except ValueError:
            continue
    return None


def normalize(v, steps):
    s = text(v)
    for step in steps:
        if step == "strip":
            s = s.strip()
        elif step == "upper":
            s = s.upper()
        elif step == "lower":
            s = s.lower()
        elif step == "collapse_spaces":
            s = " ".join(s.split())
        elif step == "digits_only":
            s = "".join(ch for ch in arabic_digits(s) if ch in "0123456789")
        elif step == "arabic_digits":
            s = arabic_digits(s)
        elif step == "arabic_text":
            s = arabic_text(s)
        elif step == "arabic_separators":
            s = arabic_separators(s)
        elif step == "arabic_names":
            s = arabic_names(s)
    return s


# ------------------------------------------------------------------ conditions

def condition_holds(cond, row):
    v = row.get(cond["column"])
    op, value = cond["op"], cond.get("value")
    if op == "blank":
        return is_blank(v)
    if op == "not_blank":
        return not is_blank(v)
    if op in ("eq", "ne", "in"):
        options = value if op == "in" else [value]
        hit = False
        for option in options:
            if is_num(v) and is_num(option):
                hit = v == option
            else:
                hit = text(v).strip().casefold() == text(option).strip().casefold()
            if hit:
                break
        return (not hit) if op == "ne" else hit
    if op in ("gt", "gte", "lt", "lte"):
        if is_num(v) and is_num(value):
            a, b = v, value
        elif isinstance(v, str) and isinstance(value, str) and not is_blank(v):
            a, b = v.strip(), value.strip()
        else:
            return False
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    if op == "contains":
        return text(value).strip().casefold() in text(v).casefold()
    if op == "starts_with":
        return text(v).strip().casefold().startswith(text(value).strip().casefold())
    raise SpecError(f"unknown condition op {op!r}")


def conditions_hold(conds, match, row):
    results = [condition_holds(c, row) for c in conds]
    return all(results) if match == "all" else any(results)


# ------------------------------------------------------------------ expressions

_TOKEN = re.compile(r"\s*(?:(\{[^{}]+\})|(\d+(?:\.\d+)?)|([-+*/()]))")


def parse_expression(expr):
    """Tokens of an arithmetic expression: {Column} references, numbers, + - * / and parentheses."""
    pos, tokens = 0, []
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise SpecError(f"cannot parse expression at {expr[pos:pos + 12]!r}")
        col, num, sym = m.groups()
        if col:
            tokens.append(("col", col[1:-1]))
        elif num:
            tokens.append(("num", float(num) if "." in num else int(num)))
        else:
            tokens.append(("sym", sym))
        pos = m.end()
    _Parser(tokens, None).parse()   # syntax check: no row, every column counts as 1
    return tokens


def expression_columns(expr):
    return [value for kind, value in parse_expression(expr) if kind == "col"]


class _Parser:
    """expr := term (('+'|'-') term)*; term := unary (('*'|'/') unary)*; unary := '-' unary | atom."""

    def __init__(self, tokens, row):
        self.tokens, self.row, self.i = tokens, row, 0

    def parse(self):
        value = self.expr()
        if self.i != len(self.tokens):
            raise SpecError("unexpected token in expression")
        return value

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def expr(self):
        value = self.term()
        while self.peek() in (("sym", "+"), ("sym", "-")):
            op = self.take()[1]
            right = self.term()
            value = None if value is None or right is None else (value + right if op == "+" else value - right)
        return value

    def term(self):
        value = self.unary()
        while self.peek() in (("sym", "*"), ("sym", "/")):
            op = self.take()[1]
            right = self.unary()
            if value is None or right is None:
                value = None
            elif op == "*":
                value = value * right
            else:
                value = None if right == 0 else value / right
        return value

    def unary(self):
        if self.peek() == ("sym", "-"):
            self.take()
            value = self.unary()
            return None if value is None else -value
        return self.atom()

    def atom(self):
        kind, value = self.take()
        if kind == "num":
            return value
        if kind == "col":
            if self.row is None:
                return 1
            v = self.row.get(value)
            return v if is_num(v) else None
        if (kind, value) == ("sym", "("):
            inner = self.expr()
            if self.take() != ("sym", ")"):
                raise SpecError("missing ')' in expression")
            return inner
        raise SpecError("expression is incomplete")


def evaluate(tokens, row):
    return _Parser(tokens, row).parse()


# ------------------------------------------------------------------ steps

def _clean_text(rows, step):
    for row in rows:
        for col in step["columns"]:
            v = row.get(col)
            if not isinstance(v, str):
                continue
            s = v.strip()
            if step.get("collapse_spaces"):
                s = " ".join(s.split())
            case = step.get("case", "none")
            if case == "lower":
                s = s.lower()
            elif case == "upper":
                s = s.upper()
            elif case == "title":
                s = s.title()
            row[col] = s if s != "" else None
    return rows


def _fill_blank(rows, step):
    col = step["column"]
    for row in rows:
        if is_blank(row.get(col)):
            row[col] = row.get(step["from_column"]) if "from_column" in step else step.get("value")
    return rows


def _map_values(rows, step):
    col, mapping = step["column"], step["mapping"]
    ci = step.get("match", "exact") == "ci"
    folded = {k.strip().casefold(): v for k, v in mapping.items()} if ci else None
    for row in rows:
        v = row.get(col)
        if is_blank(v):
            continue
        key = text(v)
        if ci:
            key = key.strip().casefold()
            if key in folded:
                row[col] = folded[key]
        elif key in mapping:
            row[col] = mapping[key]
    return rows


def _lookup(rows, step, reference):
    ref_rows = reference.get(step["reference"], [])
    steps = step.get("normalize", [])
    index = {}
    for r in ref_rows:
        k = normalize(r.get(step["reference_key"]), steps)
        if k:
            index.setdefault(k, []).append(r)
    statuses = step.get("statuses", {"found": "found", "not_found": "not_found", "ambiguous": "ambiguous"})
    overwrite = step.get("mode", "fill_blanks") == "overwrite"
    for row in rows:
        k = normalize(row.get(step["key"]), steps)
        matches = index.get(k, []) if k else []
        for out_col in step["fields"]:
            row.setdefault(out_col, None)
        if len(matches) == 1:
            match = matches[0]
            for out_col, ref_field in step["fields"].items():
                if overwrite or is_blank(row.get(out_col)):
                    row[out_col] = match.get(ref_field)
            status = statuses["found"]
        else:
            status = statuses["ambiguous"] if matches else statuses["not_found"]
        if step.get("status_column"):
            row[step["status_column"]] = status
    return rows


def _compute(rows, step):
    tokens = parse_expression(step["expression"])
    digits = step.get("round")
    for row in rows:
        value = evaluate(tokens, row)
        if value is not None and digits is not None:
            value = int(round(value)) if digits == 0 else round(value, digits)
        row[step["column"]] = value
    return rows


_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def _concat(rows, step):
    for row in rows:
        s = _PLACEHOLDER.sub(lambda m: text(row.get(m.group(1))).strip(), step["template"]).strip()
        row[step["column"]] = s if s != "" else None
    return rows


def _split(rows, step):
    into, sep = step["into"], step["separator"]
    for row in rows:
        v = row.get(step["column"])
        parts = []
        if not is_blank(v):
            s = text(v).strip()
            parts = s.split(None, len(into) - 1) if sep == " " else s.split(sep, len(into) - 1)
        for i, col in enumerate(into):
            part = parts[i].strip() if i < len(parts) else ""
            row[col] = part if part != "" else None
    return rows


def _format_date(rows, step):
    col = step["column"]
    for row in rows:
        v = row.get(col)
        out = None
        if isinstance(v, str) and v.strip():
            parsed = parse_date_value(v, step["input_formats"])
            if parsed is not None:
                out = parsed.strftime(step["output_format"])
        if out is not None:
            row[col] = out
        elif step.get("invalid", "keep") == "blank":
            row[col] = None
    return rows


def _parse_amount(rows, step):
    col = step["column"]
    for row in rows:
        v = row.get(col)
        if is_blank(v):
            continue
        parsed = parse_amount(v)
        if parsed is not None:
            row[col] = parsed
        elif step.get("invalid", "keep") == "blank":
            row[col] = None
    return rows


def _flag(rows, step):
    col = step["column"]
    for row in rows:
        row.setdefault(col, None)
        if conditions_hold(step["when"], step.get("match", "all"), row):
            row[col] = step["then"]
        elif "else" in step:
            row[col] = step["else"]
    return rows


def _filter(rows, step):
    return [row for row in rows if conditions_hold(step["keep_when"], step.get("match", "all"), row)]


def _dedupe(rows, step):
    steps = step.get("normalize", ["strip", "lower"])
    keys = [tuple(normalize(row.get(c), steps) for c in step["columns"]) for row in rows]
    keep_last = step.get("keep", "first") == "last"
    chosen = {}
    for i, key in enumerate(keys):
        if all(part == "" for part in key):
            continue
        if keep_last or key not in chosen:
            chosen[key] = i
    keep = set(chosen.values())
    return [row for i, row in enumerate(rows) if i in keep or all(part == "" for part in keys[i])]


def _sort_key(v):
    return (0, v, "") if is_num(v) else (1, 0, text(v).strip().casefold())


def _sort(rows, step):
    for key in reversed(step["by"]):
        col = key["column"]
        filled = [r for r in rows if not is_blank(r.get(col))]
        blanks = [r for r in rows if is_blank(r.get(col))]
        filled.sort(key=lambda r: _sort_key(r.get(col)), reverse=bool(key.get("descending")))
        rows = filled + blanks
    return rows


def _summarize(rows, step):
    groups = {}
    for row in rows:
        key = tuple((text(row.get(c)).strip() or "(blank)") for c in step["group_by"])
        groups.setdefault(key, []).append(row)
    digits = step.get("round")
    out = []
    for key, members in groups.items():
        record = dict(zip(step["group_by"], key))
        for agg in step["values"]:
            col, how = agg["column"], agg["agg"]
            if how == "count":
                value = sum(1 for r in members if not is_blank(r.get(col)))
            else:
                nums = [r.get(col) for r in members if is_num(r.get(col))]
                if how == "sum":
                    value = sum(nums)
                elif not nums:
                    value = None
                elif how == "avg":
                    value = sum(nums) / len(nums)
                elif how == "min":
                    value = min(nums)
                else:
                    value = max(nums)
                if value is not None and digits is not None:
                    value = int(round(value)) if digits == 0 else round(value, digits)
            record[agg["as"]] = value
        out.append(record)
    return out


ROW_OPS = {"clean_text": _clean_text, "fill_blank": _fill_blank, "map_values": _map_values, "compute": _compute,
           "concat": _concat, "split": _split, "format_date": _format_date, "parse_amount": _parse_amount,
           "flag": _flag, "filter": _filter, "dedupe": _dedupe, "sort": _sort}


def created_columns(step):
    """Columns a step adds to every row (in order), if they do not exist yet."""
    op = step["op"]
    if op in ("compute", "concat", "flag", "fill_blank"):
        return [step["column"]]
    if op == "split":
        return list(step["into"])
    if op == "lookup":
        return list(step["fields"]) + ([step["status_column"]] if step.get("status_column") else [])
    return []


def output_columns(spec, input_columns):
    """The final column order: input columns, then each new column in the order a step creates it,
    or exactly spec["output_columns"] when the spec gives it."""
    if spec.get("output_columns"):
        return list(spec["output_columns"])
    columns = list(input_columns)
    for step in spec["steps"]:
        for col in created_columns(step):
            if col not in columns:
                columns.append(col)
    return columns


def execute(spec, rows, reference):
    """{"rows": [...], "sheets": {name: [...]}} for the spec applied to rows (list of dicts)."""
    for i, row in enumerate(rows):
        for col in spec["input"]["columns"]:
            if col not in row:
                raise ValueError(f"row {i + 1} is missing column {col!r}")
    for name, ref in spec.get("references", {}).items():
        for r in reference.get(name, []):
            for field in ref["fields"]:
                if field not in r:
                    raise ValueError(f"reference {name!r} is missing field {field!r}")
    input_columns = list(rows[0]) if rows else list(spec["input"]["columns"])
    rows = copy.deepcopy(rows)
    sheets = {}
    for step in spec["steps"]:
        op = step["op"]
        if op == "lookup":
            rows = _lookup(rows, step, reference)
        elif op == "summarize":
            sheets[step["sheet"]] = _summarize(rows, step)
        else:
            rows = ROW_OPS[op](rows, step)
    columns = output_columns(spec, input_columns)
    return {"rows": [{c: row.get(c) for c in columns} for row in rows], "sheets": sheets}
