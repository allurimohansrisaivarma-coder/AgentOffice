"""The spreadsheet task spec: what the PM writes, the Engineer implements and QA tests.

A spec is an ordered list of steps from a fixed vocabulary (OP_DOCS), applied
to the rows of one sheet. Reference data for lookups comes from Zetaris (SQL
the PM writes against the catalog) or from another sheet of the same file.
`validate_spec` checks a spec against the real file before anyone builds it;
`describe` turns it into the exact semantics the Engineer must implement,
written to match app/tasks/runtime.py, which QA uses as the oracle.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Optional

from app.tasks.runtime import NORMALIZERS, SpecError, created_columns, expression_columns

CASES = ("none", "lower", "upper", "title")
COND_OPS = ("eq", "ne", "in", "gt", "gte", "lt", "lte", "contains", "starts_with", "blank", "not_blank")
AGGS = ("sum", "count", "avg", "min", "max")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
MAX_STEPS = 12

OP_DOCS = {
    "clean_text": ('{"op": "clean_text", "columns": [col, ...], "case": "none|lower|upper|title", '
                   '"collapse_spaces": true|false}',
                   "For each listed column, only where the value is a str: strip surrounding whitespace; if "
                   "collapse_spaces, replace every run of whitespace with one space (' '.join(s.split())); then apply "
                   "case with str.lower() / str.upper() / str.title() (none = unchanged). A result of '' becomes None. "
                   "Non-str values are left unchanged."),
    "fill_blank": ('{"op": "fill_blank", "column": col, "value": <constant>} or {"op": "fill_blank", "column": col, '
                   '"from_column": other_col}',
                   "Where the column is blank (None, or a str that is empty after strip), set it to the constant, or "
                   "to the row's from_column value as it is. Creates the column (with that rule) if it does not exist."),
    "map_values": ('{"op": "map_values", "column": col, "mapping": {"old": "new", ...}, "match": "exact|ci"}',
                   "Blank values are left unchanged. Otherwise let k = text(value); with match 'exact' replace the "
                   "value by mapping[k] when k is a key; with 'ci' compare k.strip().casefold() with each key's "
                   ".strip().casefold(). Values with no matching key are unchanged."),
    "lookup": ('{"op": "lookup", "key": col, "reference": ref_name, "reference_key": ref_field, '
               '"fields": {out_col: ref_field, ...}, "normalize": [..], "mode": "fill_blanks|overwrite", '
               '"status_column": col (optional), "statuses": {"found": s, "not_found": s, "ambiguous": s}}',
               "Index the reference rows by normalize(ref[reference_key]), skipping rows whose normalized key is ''. "
               "For each row: k = normalize(row[key]). Every out_col is created (None) if missing. If k is not '' "
               "and exactly one reference row matches, then for each out_col: with mode 'overwrite', or when "
               "row[out_col] is blank, set row[out_col] = match[ref_field]; status = statuses.found. If more than "
               "one matches: status = statuses.ambiguous, fields untouched. Otherwise status = statuses.not_found. "
               "If status_column is given, set row[status_column] = status."),
    "compute": ('{"op": "compute", "column": col, "expression": "{Amount} * 1.1", "round": <int digits, optional>}',
                "Evaluate the arithmetic expression: {Column} references, int/float literals, + - * /, unary minus, "
                "parentheses, usual precedence. A column reference whose value is not an int/float (bool is not a "
                "number; numeric-looking strings are NOT numbers) makes the result None; division by zero gives "
                "None. If round is given and the result is not None: round(x) as int when round == 0, else "
                "round(x, digits) with Python's built-in round. Store in column (created or overwritten)."),
    "concat": ('{"op": "concat", "column": col, "template": "{First} {Last}"}',
               "Replace each {Column} in the template with text(row[Column]).strip(), then strip the whole result; "
               "'' becomes None. Store in column (created or overwritten)."),
    "split": ('{"op": "split", "column": col, "separator": " ", "into": [col1, col2, ...]}',
              "If the value is blank every into column is None. Otherwise s = text(value).strip(); n = len(into); "
              "parts = s.split(None, n - 1) when separator is exactly ' ', else s.split(separator, n - 1). "
              "into[i] = parts[i].strip(), or None when that part is missing or ''."),
    "format_date": ('{"op": "format_date", "column": col, "input_formats": ["%d/%m/%Y", ...], '
                    '"output_format": "%Y-%m-%d", "invalid": "keep|blank"}',
                    "Only str values that are not blank: try datetime.strptime(value.strip(), fmt) for each input "
                    "format in order; the first that parses gives value = parsed.strftime(output_format). Values "
                    "that are not str, blank, or match no format: unchanged with invalid 'keep', None with 'blank'."),
    "flag": ('{"op": "flag", "column": col, "when": [condition, ...], "match": "all|any", "then": value, '
             '"else": value (optional)}',
             "Create the column (None) if missing. If the conditions hold (all of them, or any with match 'any') set "
             "column = then; otherwise set it to else when 'else' is given, else leave it unchanged."),
    "filter": ('{"op": "filter", "keep_when": [condition, ...], "match": "all|any"}',
               "Keep only the rows where the conditions hold; order is preserved."),
    "dedupe": ('{"op": "dedupe", "columns": [col, ...], "normalize": [..], "keep": "first|last"}',
               "key = tuple(normalize(row[c]) for c in columns). Rows whose key parts are all '' are always kept. "
               "Among rows sharing a key keep only the first (or last) occurrence; surviving rows keep their "
               "original relative order."),
    "sort": ('{"op": "sort", "by": [{"column": col, "descending": false}, ...]}',
             "For each sort key from LAST to FIRST: split the rows into non-blank and blank (for that column), "
             "stable-sort the non-blank rows by k(v) = (0, v, '') for int/float v, else (1, 0, "
             "text(v).strip().casefold()), with reverse=descending, then rows = sorted non-blank + blank rows in "
             "their current order (blanks always last)."),
    "summarize": ('{"op": "summarize", "sheet": name, "group_by": [col, ...], "values": [{"column": col, '
                  '"agg": "sum|count|avg|min|max", "as": out_name}, ...], "round": <int digits, optional>}',
                  "Does not change the rows. Groups the rows as they are at this step by tuple(text(row[c]).strip() "
                  "or '(blank)' for c in group_by), in order of first appearance. For each group one record: the "
                  "group_by columns (with those text values), plus for each value: count = number of rows whose "
                  "column is not blank; sum/avg/min/max over the int/float values only (sum of none = 0; avg/min/"
                  "max of none = None). round applies to sum/avg/min/max as in compute. The records go to "
                  "output sheet `sheet`."),
}

CONDITION_DOC = (
    'condition = {"column": col, "op": "eq|ne|in|gt|gte|lt|lte|contains|starts_with|blank|not_blank", '
    '"value": v (a list for in; none for blank/not_blank)}. eq: if the value and v are both int/float compare '
    "numerically, else compare text(value).strip().casefold() == text(v).strip().casefold(); ne is not eq; in is eq "
    "against any item. gt/gte/lt/lte: numeric when both are int/float; when both are str (value not blank) compare "
    "value.strip() with v.strip() as strings (ISO dates order correctly); otherwise false. contains: "
    "text(v).strip().casefold() in text(value).casefold(). starts_with: text(value).strip().casefold()"
    ".startswith(text(v).strip().casefold()). blank / not_blank: the blank rule."
)

HELPERS_DOC = (
    "Definitions: blank(v) = v is None or (isinstance(v, str) and v.strip() == ''). text(v) = '' for None; "
    "str(int(v)) for a float with v.is_integer(); otherwise str(v). Numbers are int/float but never bool. "
    "normalize(v, steps) = text(v) then each step in order: strip, upper, lower, collapse_spaces "
    "(' '.join(s.split())), digits_only (keep 0-9)."
)


def spec_format(columns_hint: str = "") -> str:
    ops = "\n".join(f"- {name}: {sig}" for name, (sig, _) in OP_DOCS.items())
    return f"""SPEC FORMAT (JSON):
{{"name": snake_case, "summary": one sentence in plain words,
 "input": {{"sheet": <sheet to process>, "columns": [<every input column the steps read>]}},
 "references": {{<ref_name>: {{"source": "zetaris", "sql": "SELECT ... FROM workforce.<table>", "fields": [..]}}
                | {{"source": "sheet", "sheet": <another sheet of the file>, "fields": [..]}}}},   (may be {{}})
 "steps": [<1-{MAX_STEPS} steps, applied in order>],
 "output_columns": [<optional: exact final column order; omit to keep input columns + new ones>]}}
STEPS:
{ops}
{CONDITION_DOC}
Column names are the sheet's header text exactly (case and spaces matter). A step may use columns created by
earlier steps. Use references only for data the sheet does not have; "fields" lists the reference fields used.
{columns_hint}"""


def _cond_errors(conds: Any, where: str, available: list[str]) -> list[str]:
    if not isinstance(conds, list) or not conds:
        return [f"{where} must be a non-empty list of conditions"]
    errors = []
    for i, c in enumerate(conds):
        w = f"{where}[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{w} must be an object")
            continue
        if c.get("column") not in available:
            errors.append(f"{w}.column {c.get('column')!r} is not a column at this step")
        if c.get("op") not in COND_OPS:
            errors.append(f"{w}.op must be one of {list(COND_OPS)}")
        elif c["op"] == "in" and not isinstance(c.get("value"), list):
            errors.append(f"{w}.value must be a list for op 'in'")
        elif c["op"] not in ("blank", "not_blank", "in") and "value" not in c:
            errors.append(f"{w} needs a value")
    return errors


def _step_errors(step: dict, where: str, available: list[str], references: dict, sheet_names: set[str],
                 summary_sheets: set[str]) -> list[str]:
    op = step["op"]
    errors: list[str] = []

    def need_col(key: str) -> None:
        if step.get(key) not in available:
            errors.append(f"{where}.{key} {step.get(key)!r} is not a column at this step; columns: {available}")

    def need_new(key: str) -> None:
        if not isinstance(step.get(key), str) or not step[key].strip():
            errors.append(f"{where}.{key} must be a column name")

    if op == "clean_text":
        cols = step.get("columns")
        if not isinstance(cols, list) or not cols:
            errors.append(f"{where}.columns must be a non-empty list")
        else:
            errors += [f"{where}.columns: {c!r} is not a column at this step" for c in cols if c not in available]
        if step.get("case", "none") not in CASES:
            errors.append(f"{where}.case must be one of {list(CASES)}")
    elif op == "fill_blank":
        need_new("column")
        if ("value" in step) == ("from_column" in step):
            errors.append(f"{where} needs exactly one of 'value' or 'from_column'")
        elif "from_column" in step:
            need_col("from_column")
    elif op == "map_values":
        need_col("column")
        if not isinstance(step.get("mapping"), dict) or not step["mapping"]:
            errors.append(f"{where}.mapping must be a non-empty object")
        if step.get("match", "exact") not in ("exact", "ci"):
            errors.append(f"{where}.match must be 'exact' or 'ci'")
    elif op == "lookup":
        need_col("key")
        ref = references.get(step.get("reference"))
        if ref is None:
            errors.append(f"{where}.reference must name one of the spec's references: {sorted(references)}")
        else:
            fields = ref.get("fields", [])
            if step.get("reference_key") not in fields:
                errors.append(f"{where}.reference_key must be one of reference fields {fields}")
            out = step.get("fields")
            if not isinstance(out, dict) or not out:
                errors.append(f"{where}.fields must map output columns to reference fields")
            else:
                errors += [f"{where}.fields: {f!r} is not a field of reference {step['reference']!r}"
                           for f in out.values() if f not in fields]
        if not isinstance(step.get("normalize", []), list) or any(n not in NORMALIZERS
                                                                  for n in step.get("normalize", [])):
            errors.append(f"{where}.normalize must be a list drawn from {list(NORMALIZERS)}")
        if step.get("mode", "fill_blanks") not in ("fill_blanks", "overwrite"):
            errors.append(f"{where}.mode must be 'fill_blanks' or 'overwrite'")
        statuses = step.get("statuses")
        if statuses is not None and (not isinstance(statuses, dict) or set(statuses) != {"found", "not_found",
                                                                                          "ambiguous"}):
            errors.append(f"{where}.statuses must have exactly found, not_found and ambiguous")
    elif op == "compute":
        need_new("column")
        try:
            missing = [c for c in expression_columns(str(step.get("expression", ""))) if c not in available]
            errors += [f"{where}.expression uses {c!r}, which is not a column at this step" for c in missing]
        except SpecError as e:
            errors.append(f"{where}.expression: {e}")
        if "round" in step and (not isinstance(step["round"], int) or isinstance(step["round"], bool)
                                or not 0 <= step["round"] <= 6):
            errors.append(f"{where}.round must be an integer 0-6")
    elif op == "concat":
        need_new("column")
        refs = re.findall(r"\{([^{}]+)\}", str(step.get("template", "")))
        if not refs:
            errors.append(f"{where}.template must reference at least one {{Column}}")
        errors += [f"{where}.template uses {c!r}, which is not a column at this step" for c in refs
                   if c not in available]
    elif op == "split":
        need_col("column")
        if not isinstance(step.get("separator"), str) or not step["separator"]:
            errors.append(f"{where}.separator must be a non-empty string")
        into = step.get("into")
        if not isinstance(into, list) or not 2 <= len(into) <= 6 or not all(isinstance(c, str) and c for c in into):
            errors.append(f"{where}.into must list 2-6 new column names")
    elif op == "format_date":
        need_col("column")
        formats = step.get("input_formats")
        if not isinstance(formats, list) or not formats or not all(isinstance(f, str) and "%" in f for f in formats):
            errors.append(f"{where}.input_formats must be a non-empty list of strptime formats")
        if not isinstance(step.get("output_format"), str) or "%" not in step.get("output_format", ""):
            errors.append(f"{where}.output_format must be a strftime format")
        if step.get("invalid", "keep") not in ("keep", "blank"):
            errors.append(f"{where}.invalid must be 'keep' or 'blank'")
    elif op == "flag":
        need_new("column")
        errors += _cond_errors(step.get("when"), f"{where}.when", available)
        if "then" not in step:
            errors.append(f"{where} needs 'then'")
    elif op == "filter":
        errors += _cond_errors(step.get("keep_when"), f"{where}.keep_when", available)
    elif op == "dedupe":
        cols = step.get("columns")
        if not isinstance(cols, list) or not cols:
            errors.append(f"{where}.columns must be a non-empty list")
        else:
            errors += [f"{where}.columns: {c!r} is not a column at this step" for c in cols if c not in available]
        if step.get("keep", "first") not in ("first", "last"):
            errors.append(f"{where}.keep must be 'first' or 'last'")
    elif op == "sort":
        by = step.get("by")
        if not isinstance(by, list) or not by:
            errors.append(f"{where}.by must be a non-empty list")
        else:
            errors += [f"{where}.by: {k!r} needs a column at this step" for k in by
                       if not isinstance(k, dict) or k.get("column") not in available]
    elif op == "summarize":
        name = step.get("sheet")
        if not isinstance(name, str) or not name.strip() or len(name) > 31:
            errors.append(f"{where}.sheet must be a sheet name of 1-31 characters")
        elif name in sheet_names or name in summary_sheets:
            errors.append(f"{where}.sheet {name!r} is already used")
        else:
            summary_sheets.add(name)
        group = step.get("group_by")
        if not isinstance(group, list) or not group or any(c not in available for c in group):
            errors.append(f"{where}.group_by must list columns at this step")
        values = step.get("values")
        if not isinstance(values, list) or not values:
            errors.append(f"{where}.values must be a non-empty list")
        else:
            for i, v in enumerate(values):
                if not isinstance(v, dict) or v.get("column") not in available or v.get("agg") not in AGGS \
                        or not isinstance(v.get("as"), str) or not v["as"]:
                    errors.append(f"{where}.values[{i}] needs a column at this step, agg in {list(AGGS)} and 'as'")
        if "round" in step and (not isinstance(step["round"], int) or not 0 <= step["round"] <= 6):
            errors.append(f"{where}.round must be an integer 0-6")
    return errors


def validate_spec(spec: Any, sheets: dict[str, list[str]], *, catalog_tables: Optional[set[str]] = None) -> list[str]:
    """Every problem with a spec against the uploaded file ({sheet: columns}). Empty means valid."""
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]
    errors = [f"missing '{k}'" for k in ("name", "summary", "input", "steps") if k not in spec]
    if errors:
        return errors
    if not _NAME.match(str(spec["name"])):
        errors.append("name must be snake_case (a-z, 0-9, _), max 41 chars")
    inp = spec["input"]
    if not isinstance(inp, dict) or inp.get("sheet") not in sheets:
        return errors + [f"input.sheet must be one of the file's sheets: {sorted(sheets)}"]
    columns = sheets[inp["sheet"]]
    declared = inp.get("columns")
    if not isinstance(declared, list) or not declared:
        errors.append("input.columns must list the input columns the steps read")
        declared = []
    errors += [f"input.columns: {c!r} is not a column of sheet {inp['sheet']!r}; columns: {columns}"
               for c in declared if c not in columns]

    references = spec.get("references", {}) or {}
    if not isinstance(references, dict):
        errors.append("references must be an object")
        references = {}
    for name, ref in references.items():
        where = f"references.{name}"
        if not _NAME.match(str(name)):
            errors.append(f"{where}: reference names must be snake_case")
        if not isinstance(ref, dict) or ref.get("source") not in ("zetaris", "sheet"):
            errors.append(f"{where}.source must be 'zetaris' or 'sheet'")
            continue
        if not isinstance(ref.get("fields"), list) or not ref["fields"]:
            errors.append(f"{where}.fields must list the reference fields used")
        if ref["source"] == "sheet":
            if ref.get("sheet") not in sheets or ref.get("sheet") == inp["sheet"]:
                errors.append(f"{where}.sheet must be another sheet of the file: {sorted(set(sheets) - {inp['sheet']})}")
            else:
                errors += [f"{where}.fields: {f!r} is not a column of sheet {ref['sheet']!r}"
                           for f in ref.get("fields", []) if f not in sheets[ref["sheet"]]]
        else:
            sql = str(ref.get("sql", ""))
            if not re.match(r"(?is)^\s*select\b", sql) or ";" in sql.strip().rstrip(";"):
                errors.append(f"{where}.sql must be one SELECT statement")
            used = set(re.findall(r"(?i)\bworkforce\.([a-z_][a-z0-9_]*)", sql))
            if not used:
                errors.append(f"{where}.sql must read a workforce.<table> from the catalog")
            elif catalog_tables is not None and used - catalog_tables:
                errors.append(f"{where}.sql reads unknown tables {sorted(used - catalog_tables)}")

    steps = spec["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return errors + [f"steps must be a list of 1 to {MAX_STEPS} steps"]
    available = list(columns)
    summary_sheets: set[str] = set()
    for i, step in enumerate(steps):
        where = f"steps[{i}]"
        if not isinstance(step, dict) or step.get("op") not in OP_DOCS:
            errors.append(f"{where}.op must be one of {list(OP_DOCS)}")
            continue
        errors += _step_errors(step, where, available, references, set(sheets), summary_sheets)
        for col in _safe_created(step):
            if col not in available:
                available.append(col)
    out = spec.get("output_columns")
    if out is not None:
        if not isinstance(out, list) or not out:
            errors.append("output_columns must be a non-empty list or omitted")
        else:
            errors += [f"output_columns: {c!r} is not a column after the steps" for c in out if c not in available]
            if len(set(out)) != len(out):
                errors.append("output_columns has duplicates")
    return errors


def _safe_created(step: dict) -> list[str]:
    try:
        return [c for c in created_columns(step) if isinstance(c, str)]
    except (KeyError, TypeError):
        return []


def describe(spec: dict) -> str:
    """The exact semantics of this spec, in words, for the Engineer (only the ops it uses)."""
    used = []
    for step in spec["steps"]:
        if step["op"] not in used:
            used.append(step["op"])
    refs = spec.get("references", {}) or {}
    lines = [
        f"Automation `{spec['name']}`: {spec['summary']}",
        "Implement run(rows, reference) -> {\"rows\": [...], \"sheets\": {...}}.",
        "rows: list of dicts, one per spreadsheet row, keys = column headers in sheet order; values are str, int, "
        "float or None. reference: dict of reference name -> list of dicts. Do not mutate the inputs.",
        f"First, if any row lacks one of the input columns {json.dumps(spec['input']['columns'])}, raise ValueError "
        "naming the column" + (", and if any reference row lacks one of its fields, raise ValueError naming it"
                               if refs else "") + " (a renamed column must fail loudly, never be guessed).",
        "References: " + (", ".join(f"{n} with fields {json.dumps(r['fields'])}" for n, r in refs.items()) or "none")
        + ".",
        HELPERS_DOC,
        "Apply these steps in order:",
    ]
    for i, step in enumerate(spec["steps"], start=1):
        lines.append(f"  {i}. {json.dumps(step)}")
    lines.append("Step semantics:")
    for op in used:
        lines.append(f"  - {op}: {OP_DOCS[op][1]}")
    if any(op in ("flag", "filter") for op in used):
        lines.append(f"  - {CONDITION_DOC}")
    if spec.get("output_columns"):
        cols = f"exactly these columns in this order: {json.dumps(spec['output_columns'])}"
    else:
        cols = ("the input columns in their original order followed by each new column in the order a step first "
                "creates it")
    lines.append(f"Output \"rows\": every remaining row as a dict with {cols} (None where a row has no value). "
                 "Output \"sheets\": one entry per summarize step, sheet name -> its list of records "
                 "(an empty dict when there is none).")
    return "\n".join(lines)


# ------------------------------------------------------------------ demo specs (sample mode planner)

DEMO_SPECS: dict[str, dict] = {
    "expense_claims": {
        "name": "expense_claims_filler",
        "summary": "Fill each claim's team and role from the staff directory in Zetaris, tidy categories, "
                   "standardise dates, add GST and flag large claims, with totals by team.",
        "input": {"sheet": "Claims", "columns": ["Claim ID", "Employee Email", "Date", "Category", "Amount"]},
        "references": {"staff": {"source": "zetaris", "sql": "SELECT email, team, role FROM workforce.people",
                                 "fields": ["email", "team", "role"]}},
        "steps": [
            {"op": "clean_text", "columns": ["Category"], "case": "title", "collapse_spaces": True},
            {"op": "lookup", "key": "Employee Email", "reference": "staff", "reference_key": "email",
             "fields": {"Team": "team", "Role": "role"}, "normalize": ["strip", "lower"], "mode": "fill_blanks",
             "status_column": "Directory Match",
             "statuses": {"found": "matched", "not_found": "not in directory", "ambiguous": "check manually"}},
            {"op": "format_date", "column": "Date", "input_formats": ["%Y-%m-%d", "%d/%m/%Y", "%d %b %Y"],
             "output_format": "%Y-%m-%d", "invalid": "keep"},
            {"op": "compute", "column": "Amount incl GST", "expression": "{Amount} * 1.1", "round": 2},
            {"op": "flag", "column": "Needs Approval", "when": [{"column": "Amount", "op": "gt", "value": 500}],
             "match": "all", "then": "Yes", "else": "No"},
            {"op": "summarize", "sheet": "Totals by Team", "group_by": ["Team"],
             "values": [{"column": "Claim ID", "agg": "count", "as": "Claims"},
                        {"column": "Amount", "agg": "sum", "as": "Total Amount"}], "round": 2},
        ],
    },
    "vendor_invoices": {
        "name": "invoice_register_cleanup",
        "summary": "Remove duplicate invoices, fill missing statuses, mark unpaid invoices past the cut-off as "
                   "overdue and sort by due date.",
        "input": {"sheet": "vendor_invoices", "columns": ["Invoice No", "Vendor", "Due Date", "Status"]},
        "references": {},
        "steps": [
            {"op": "clean_text", "columns": ["Vendor"], "case": "none", "collapse_spaces": True},
            {"op": "dedupe", "columns": ["Invoice No"], "normalize": ["strip", "upper"], "keep": "first"},
            {"op": "fill_blank", "column": "Status", "value": "Pending"},
            {"op": "flag", "column": "Status", "when": [{"column": "Due Date", "op": "lt", "value": "2026-10-15"},
                                                        {"column": "Status", "op": "ne", "value": "Paid"}],
             "match": "all", "then": "Overdue"},
            {"op": "sort", "by": [{"column": "Due Date", "descending": False}]},
        ],
    },
    "leads_and_contacts": {
        "name": "lead_crm_checker",
        "summary": "Split lead names, check every lead against the CRM contacts sheet by email, fill the contact "
                   "owner and say which leads need a new contact.",
        "input": {"sheet": "Leads", "columns": ["Lead ID", "Full Name", "Email"]},
        "references": {"crm": {"source": "sheet", "sheet": "CRM Contacts", "fields": ["Email", "Owner"]}},
        "steps": [
            {"op": "split", "column": "Full Name", "separator": " ", "into": ["First Name", "Last Name"]},
            {"op": "lookup", "key": "Email", "reference": "crm", "reference_key": "Email",
             "fields": {"Owner": "Owner"}, "normalize": ["strip", "lower"], "mode": "fill_blanks",
             "status_column": "CRM Status",
             "statuses": {"found": "Already in CRM", "not_found": "Create contact",
                          "ambiguous": "Duplicate in CRM"}},
        ],
    },
}


def demo_spec_for(sheets: dict[str, list[str]]) -> Optional[dict]:
    """The bundled demo spec whose input sheet and columns this file has (sample mode only)."""
    for spec in DEMO_SPECS.values():
        cols = sheets.get(spec["input"]["sheet"])
        if cols is not None and set(spec["input"]["columns"]) <= set(cols) and not validate_spec(spec, sheets):
            return copy.deepcopy(spec)
    return None
