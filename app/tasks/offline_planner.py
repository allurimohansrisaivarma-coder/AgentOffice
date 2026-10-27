"""Sample-mode PM for any spreadsheet: a rule-based planner, no model.

The request is cut into clauses ("remove duplicates, tidy the names and sort by
date" -> three). Each clause is matched to one step type from OP_DOCS using the
file profile (exact column names, types, values). A step is kept only if the
spec still passes `validate_spec` with it; a clause nothing matches is reported
as left out, never guessed. The Spec Critic and QA then check the plan against
the real file exactly as they check a model's plan.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Optional

from app.tasks.runtime import created_columns, expression_columns
from app.tasks.spec import MAX_STEPS, validate_spec

ORIGIN = "rule-based planner (sample mode)"

# What the rule planner understands, shown when it can't plan a request.
UNDERSTOOD = (
    "remove duplicates (by <column>)", "tidy / trim / title-case <columns>", "standardise the <date column> dates",
    "fill blank <column> with <value>", "replace <old> with <new> in <column>",
    "split <column> into first and last name", "combine <A> and <B> into <new column>",
    "look up team and role from the staff directory (needs an email column)",
    "match against the <other sheet> sheet", "add 10% GST to <amount>", "multiply <A> by <B>",
    "flag <amount> over 500", "keep only rows where <column> is <value>", "remove rows with blank <column>",
    "sort by <column> (descending)", "totals by <column>")

_VERBS = (r"(?:clean|tidy|trim|remove|drop|delete|dedupe|de-?dup\w*|fill|look|lookup|match|check|format|convert|"
          r"standardi[sz]e|normali[sz]e|add|compute|calculate|work out|flag|mark|highlight|keep|filter|exclude|sort|"
          r"order|rank|total|sum|summari[sz]e|group|count|split|separate|combine|join|merge|concatenate|replace|"
          r"change|map|rename|capitali[sz]e|uppercase|lowercase|put|make|give|show|then)")
_SPLIT = re.compile(r"[;\n]|,(?!\d)|\.(?=\s|$)|\bthen\b|\b(?:and|also|plus)\b(?=\s+(?:then\s+)?" + _VERBS + r"\b)",
                    re.I)
_AMOUNT = re.compile(r"(?i)amount|total|price|cost|value|salary|hours|qty|quantity|fee|spend|revenue|balance|sales")
_ID = re.compile(r"(?i)(^|[\s_])(id|no|num|number|ref|reference|code|#)$|e-?mail")
_DATE_NAME = re.compile(r"(?i)date|\bdob\b|_at$|\bday\b|\bwhen\b|deadline|due")
_DATE_SHAPE = re.compile(r"^\s*\d{1,4}[-/. ][A-Za-z0-9]{1,9}[-/. ,]+\d{1,4}")
_COMPARE = re.compile(r"(?i)(more than|greater than|over|above|exceed(?:s|ing)?|at least|less than|fewer than|under|"
                      r"below|at most|before|after|>=|<=|>|<)\s*[$£€]?\s*(\d{4}-\d{2}-\d{2}|\d[\d,]*(?:\.\d+)?)")
_OPS = {"more than": "gt", "greater than": "gt", "over": "gt", "above": "gt", "exceed": "gt", "exceeds": "gt",
        "exceeding": "gt", "at least": "gte", "less than": "lt", "fewer than": "lt", "under": "lt", "below": "lt",
        "at most": "lte", "before": "lt", "after": "gt", ">=": "gte", "<=": "lte", ">": "gt", "<": "lt"}
_INVERSE = {"gt": "lte", "gte": "lt", "lt": "gte", "lte": "gt", "eq": "ne", "ne": "eq", "blank": "not_blank",
            "not_blank": "blank"}
_OP_WORDS = {"gt": "over", "gte": "at least", "lt": "under", "lte": "at most", "eq": "is", "ne": "is not"}
DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%b %d, %Y",
                "%B %d, %Y", "%b %d %Y", "%Y/%m/%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]
US_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%d %b %Y", "%d %B %Y",
                   "%Y/%m/%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]
# Steps that read best first or last, whatever order the request names them in; the rest keep the request's order.
_RANK = {"clean_text": 0, "dedupe": 1, "sort": 8, "summarize": 9}


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ses", "xes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _words(s: str) -> str:
    return " " + " ".join(_stem(w) for w in re.findall(r"[a-z0-9#]+", str(s).casefold())) + " "


def _mentions(clause: str, columns: list[str]) -> list[str]:
    """Columns named in the clause (plural or singular, any case), in the order they appear; a longer name wins
    over one it contains ('Due Date' over 'Date')."""
    text, found = _words(clause), []
    for col in sorted(columns, key=lambda c: -len(_words(c))):
        phrase = _words(col)
        if phrase.strip() and phrase in text:
            found.append((text.index(phrase), col))
            text = text.replace(phrase, " " + "~" * (len(phrase) - 2) + " ")
    return [c for _, c in sorted(found)]


def _without(clause: str, columns: list[str]) -> str:
    """The clause in lower case with the named columns blanked out (plural forms too)."""
    low = clause.casefold()
    for col in sorted(columns, key=len, reverse=True):
        words = re.findall(r"[a-z0-9#]+", col.casefold())
        if words:
            pattern = r"\b" + r"[\s_\-]+".join(re.escape(w.rstrip("y") or w) + r"\w{0,3}" for w in words) + r"\b"
            low = re.sub(pattern, " ", low)
    return low


def _literal(raw: str) -> Any:
    raw = raw.strip().strip("'\"").strip()
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d*\.\d+", raw):
        return float(raw)
    return raw


def _name(task: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", task.casefold()) if w not in ("the", "a", "an", "my", "of", "to",
                                                                             "please", "and", "in", "for", "each")]
    name = "_".join(w[:12] for w in words[:4]) or "sheet_task"
    name = name if name[0].isalpha() else "task_" + name
    return name[:41].rstrip("_")


class _Planner:
    def __init__(self, task: str, profile: dict, sheets: dict[str, list[str]], catalog: list[dict]):
        self.task, self.sheets = task, sheets
        self.sheet = self._pick_sheet(profile)
        self.columns = list(sheets[self.sheet])
        info = next((s for s in profile.get("sheets", []) if s["name"] == self.sheet), {"columns": []})
        self.info = {c["name"]: c for c in info["columns"]}
        self.people = next((t for t in catalog if t["table"] == "people"), None)
        self.available = list(self.columns)
        self.numeric_new: set[str] = set()
        self.references: dict[str, dict] = {}
        self.notes: list[str] = []

    def _pick_sheet(self, profile: dict) -> str:
        task = _words(self.task)
        named = [s for s in self.sheets if _words(s).strip() and _words(s) in task]
        if len(named) == 1:
            return named[0]
        rows = {s["name"]: s.get("rows", 0) for s in profile.get("sheets", [])}
        return max(self.sheets, key=lambda s: (len(_mentions(self.task, self.sheets[s])), rows.get(s, 0) > 0,
                                               -list(self.sheets).index(s)))

    # ------------------------------------------------------------ column kinds

    def numeric(self, col: str) -> bool:
        return col in self.numeric_new or self.info.get(col, {}).get("type") in ("number", "mixed")

    def date_like(self, col: str) -> bool:
        if col not in self.columns or self.numeric(col):
            return False
        examples = [e for e in self.info.get(col, {}).get("examples", []) if isinstance(e, str)]
        return bool(_DATE_NAME.search(col)) or bool(examples) and all(_DATE_SHAPE.match(e) for e in examples)

    def email_like(self, col: str) -> bool:
        examples = self.info.get(col, {}).get("examples", [])
        return "mail" in col.casefold() or any(isinstance(e, str) and "@" in e for e in examples)

    def amount(self, mentioned: list[str]) -> Optional[str]:
        nums = [c for c in mentioned if self.numeric(c)]
        if nums:
            return nums[0]
        nums = [c for c in self.available if self.numeric(c)]
        named = [c for c in nums if _AMOUNT.search(c)]
        if named:
            return named[0]
        return nums[0] if len(nums) == 1 else None

    def values_of(self, col: str) -> list[str]:
        out = []
        for entry in self.info.get(col, {}).get("values", []):
            try:
                out.append(str(ast.literal_eval(entry.rsplit(" x", 1)[0])))
            except (ValueError, SyntaxError):
                pass
        return out

    # ------------------------------------------------------------ conditions

    def condition(self, clause: str, mentioned: list[str]) -> Optional[tuple[dict, str]]:
        low = clause.casefold()
        m = _COMPARE.search(clause)
        if m:
            raw, op = m.group(2), _OPS[m.group(1).casefold()]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                col = next((c for c in mentioned if self.date_like(c)), None) or next(
                    (c for c in self.columns if self.date_like(c)), None)
                if col is None:
                    return None
                self.notes.append(f"dates in {col!r} are compared as text, which orders correctly for ISO dates")
                return {"column": col, "op": op, "value": raw}, f"{col} {m.group(1).casefold()} {raw}"
            col = self.amount(mentioned)
            if col is None:
                return None
            value = _literal(raw.replace(",", ""))
            return {"column": col, "op": op, "value": value}, f"{col} {_OP_WORDS[op]} {raw}"
        blank = re.search(r"\b(blank|empty|missing|no|without)\b", low)
        if blank and mentioned:
            return {"column": mentioned[0], "op": "blank"}, f"{mentioned[0]} is blank"
        m = re.search(r"\bwhere\s+(.+?)\s+(is not|isn't|!=|is|=|==|equals?)\s+(.+)$", clause, re.I)
        if m:
            cols = _mentions(m.group(1), self.available)
            if cols:
                op = "ne" if m.group(2).casefold() in ("is not", "isn't", "!=") else "eq"
                value = _literal(m.group(3))
                return {"column": cols[0], "op": op, "value": value}, f"{cols[0]} {_OP_WORDS[op]} {value}"
        for col in mentioned + [c for c in self.available if c not in mentioned]:
            for value in self.values_of(col):
                if len(value) > 1 and _words(value).strip() and _words(value) in _words(clause) \
                        and _words(value) != _words(col):
                    return {"column": col, "op": "eq", "value": value}, f"{col} is {value}"
        return None

    # ------------------------------------------------------------ rules (first match wins per clause)

    def summarize(self, clause: str, low: str, mentioned: list[str]) -> Optional[list[tuple[dict, str]]]:
        if not re.search(r"\b(totals?|subtotals?|sums?|summar\w*|group(?:ed)?|counts?|breakdown|break down|"
                         r"averages?|avg)\b", low):
            return None
        by = re.search(r"\b(?:by|per|for each|for every|grouped by)\s+(.+)$", clause, re.I)
        groups = _mentions(by.group(1), self.available) if by else []
        if not groups:
            return None
        head = clause[:by.start()]
        nums = [c for c in _mentions(head, self.available) if self.numeric(c) and c not in groups]
        if not nums:
            nums = [c for c in [self.amount([])] if c and c not in groups]
        agg = "avg" if re.search(r"\b(average|avg|mean)\b", low) else "sum"
        values = [{"column": self.columns[0], "agg": "count", "as": "Rows"}]
        values += [{"column": c, "agg": agg, "as": f"{'Average' if agg == 'avg' else 'Total'} {c}"} for c in nums[:3]]
        sheet = f"Totals by {', '.join(groups)}"[:31]
        step = {"op": "summarize", "sheet": sheet, "group_by": groups, "values": values, "round": 2}
        return [(step, f"totals by {', '.join(groups)} on a new sheet {sheet!r}")]

    def lookup(self, clause: str, low: str, mentioned: list[str]) -> Optional[list[tuple[dict, str]]]:
        others = [s for s in self.sheets if s != self.sheet and _words(s).strip() and _words(s) in _words(clause)]
        if others:
            other = others[0]
            other_cols = self.sheets[other]
            shared = [c for c in self.columns if c.casefold() in {o.casefold() for o in other_cols}]
            key = next((c for c in shared if self.email_like(c)), None) or next(
                (c for c in shared if _ID.search(c)), None) or (shared[0] if shared else None)
            if key is None:
                return None
            ref_key = next(o for o in other_cols if o.casefold() == key.casefold())
            wanted = [o for o in _mentions(clause, other_cols) if o != ref_key]
            if not wanted:
                wanted = [o for o in other_cols if o.casefold() not in {c.casefold() for c in self.columns}][:3]
            fields = {next((c for c in self.columns if c.casefold() == o.casefold()), o): o for o in wanted}
            ref = re.sub(r"[^a-z0-9]+", "_", other.casefold()).strip("_") or "other_sheet"
            ref = ref if ref[0].isalpha() else "sheet_" + ref
            self.references[ref] = {"source": "sheet", "sheet": other, "fields": [ref_key] + list(fields.values())}
            step = {"op": "lookup", "key": key, "reference": ref, "reference_key": ref_key, "fields": fields,
                    "normalize": ["strip", "lower"], "mode": "fill_blanks", "status_column": f"In {other}"[:40],
                    "statuses": {"found": "found", "not_found": "not found", "ambiguous": "check manually"}}
            what = ", ".join(fields) if fields else "a match status"
            return [(step, f"match each row to the {other!r} sheet by {key} and fill {what}")]
        if self.people is None or not re.search(r"\b(directory|staff|employees?|people|hr|zetaris|look\s?up|lookup|"
                                                r"company data)\b", low):
            return None
        key = next((c for c in mentioned if self.email_like(c)), None) or next(
            (c for c in self.columns if self.email_like(c)), None)
        if key is None:
            self.notes.append("a staff-directory lookup needs an email column in the sheet")
            return None
        people = [c["name"] for c in self.people["columns"] if c["name"] not in ("email", "person_id")]
        wanted = [p for p in people if _words(p) in _words(clause)] or [p for p in ("team", "role") if p in people]
        fields = {next((c for c in self.columns if c.casefold() == p.casefold()), p.title()): p for p in wanted}
        self.references["staff"] = {"source": "zetaris", "sql": f"SELECT email, {', '.join(wanted)} FROM "
                                                                f"{self.people['ref']}", "fields": ["email"] + wanted}
        step = {"op": "lookup", "key": key, "reference": "staff", "reference_key": "email", "fields": fields,
                "normalize": ["strip", "lower"], "mode": "fill_blanks", "status_column": "Directory Match",
                "statuses": {"found": "matched", "not_found": "not in directory", "ambiguous": "check manually"}}
        return [(step, f"fill {' and '.join(fields)} from the staff directory in Zetaris by {key}")]

    def dedupe(self, clause: str, low: str, mentioned: list[str]) -> Optional[list[tuple[dict, str]]]:
        if not re.search(r"duplicat|\bdedup|de-dup|\bunique\b", low):
            return None
        cols = mentioned or [c for c in self.columns if _ID.search(c)][:1] or list(self.columns)
        keep = "last" if re.search(r"\b(last|latest|newest|most recent)\b", low) else "first"
        step = {"op": "dedupe", "columns": cols, "normalize": ["strip", "lower"], "keep": keep}
        return [(step, f"remove duplicate rows (same {', '.join(cols)}), keeping the {keep}")]

    def split(self, clause: str, low: str, mentioned: list[str]) -> Optional[list[tuple[dict, str]]]:
        if not re.search(r"\b(split|separate)\b", low):
            return None
        col = (mentioned or [c for c in self.columns if "name" in c.casefold()] or [None])[0]
        if col is None or col not in self.available:
            return None
        into_m = re.search(r"\binto\s+(.+)$", clause, re.I)
        into = []
        if re.search(r"\bfirst\b", low) and re.search(r"\b(last|sur)\s?name|\blast\b", low):
            into = ["First Name", "Last Name"]
        elif into_m:
            into = [p.strip(" '\"") for p in re.split(r",|\band\b", into_m.group(1)) if p.strip(" '\"")]
            into = [p for p in into if p.casefold() not in ("column", "columns", "two", "parts")]
        if not 2 <= len(into) <= 6:
            into = [f"{col} 1", f"{col} 2"]
        sep = "," if "comma" in low else "-" if re.search(r"dash|hyphen", low) else "/" if "slash" in low else " "
        step = {"op": "split", "column": col, "separator": sep, "into": into}
        return [(step, f"split {col} into {', '.join(into)}")]

    def concat(self, clause: str, low: str, mentioned: list[str]) -> Optional[list[tuple[dict, str]]]:
        if not re.search(r"\b(combine|concatenat\w*|join|merge)\b", low):
            return None
        target = re.search(r"\b(?:into|as)\s+(?:a\s+)?(?:new\s+)?(?:column\s+)?['\"]?([\w][\w ]*?)['\"]?"
                           r"(?:\s+column)?\s*$", clause, re.I)
        sources = [c for c in mentioned if c in self.available]
        name = target.group(1).strip() if target else None
        if name:
            sources = [c for c in sources if c != name]
        if len(sources) < 2:
            return None
        sep = ", " if "comma" in low else " - " if re.search(r"dash|hyphen", low) else " "
        name = name or " ".join(sources)
        step = {"op": "concat", "column": name, "template": sep.join(f"{{{c}}}" for c in sources)}
        return [(step, f"combine {' and '.join(sources)} into {name}")]

    def sort(self, clause: str, low: str, mentioned: list[str]) -> Optional[list[tuple[dict, str]]]:
        if not re.search(r"\b(sort|order|rank)\b", low):
            return None
        by = re.search(r"\b(?:by|on)\s+(.+)$", clause, re.I)
        cols = _mentions(by.group(1), self.available) if by else mentioned
        if not cols:
            return None
        desc = bool(re.search(r"desc|highest|largest|biggest|newest|latest|most recent|\bmost\b|z\s?-\s?a|"
                              r"high to low|big to small", low))
        step = {"op": "sort", "by": [{"column": c, "descending": desc} for c in cols]}
        return [(step, f"sort by {', '.join(cols)}{' (descending)' if desc else ''}")]

    def flag(self, clause: str, low: str, mentioned: list[str]) -> Optional[list[tuple[dict, str]]]:
        if not re.search(r"\b(flag|mark|highlight|label|tag)\b", low):
            return None
        found = self.condition(clause, mentioned)
        if found is None:
            if re.search(r"\b(large|big|high|small|low|old|late)\b", low):
                self.notes.append(f"{clause.strip()!r}: give a threshold offline, e.g. 'flag amounts over 500'")
            return None
        cond, words = found
        then_m = re.search(r"\bas\s+['\"]?([A-Za-z][\w ]*?)['\"]?\s*$", clause)
        if then_m and not _mentions(then_m.group(1), self.available):
            then, other = then_m.group(1).strip(), None
            column = "Flag"
        else:
            then, other = "Yes", "No"
            column = ("Needs Approval" if "approv" in low else "Needs Review" if "review" in low
                      else words[:40].strip())
        step = {"op": "flag", "column": column, "when": [cond], "match": "all", "then": then}
        if other is not None:
            step["else"] = other
        return [(step, f"mark rows where {words} ({column} = {then})")]

    def filter(self, clause: str, low: str, mentioned: list[str]) -> Optional[list[tuple[dict, str]]]:
        if not re.search(r"\b(keep|filter|exclude|remove|drop|delete|only|discard)\b", low):
            return None
        found = self.condition(clause, mentioned)
        if found is None:
            return None
        cond, words = found
        if re.search(r"\b(exclude|remove|drop|delete|discard)\b", low):
            if cond["op"] not in _INVERSE:
                return None
            cond = dict(cond, op=_INVERSE[cond["op"]])
            words = f"not ({words})"
        step = {"op": "filter", "keep_when": [cond], "match": "all"}
        return [(step, f"keep only rows where {words}")]

    def compute(self, clause: str, low: str, mentioned: list[str]) -> Optional[list[tuple[dict, str]]]:
        nums = [c for c in mentioned if self.numeric(c)]
        target = re.search(r"\b(?:as|into|called|named)\s+['\"]?([A-Za-z][\w %]*?)['\"]?\s*$", clause)
        pair = [(r"\b(multiply|multiplied|times|product)\b", "*", "x"), (r"\b(divide|divided|ratio)\b", "/", "per"),
                (r"\b(minus|subtract|difference)\b", "-", "minus"), (r"\b(plus|sum of)\b", "+", "plus")]
        for pattern, sign, word in pair:
            if re.search(pattern, low) and len(nums) >= 2:
                a, b = nums[0], nums[1]
                name = target.group(1).strip() if target else f"{a} {word} {b}"
                step = {"op": "compute", "column": name, "expression": f"{{{a}}} {sign} {{{b}}}", "round": 2}
                self.numeric_new.add(name)
                return [(step, f"{name} = {a} {sign} {b}")]
        pct = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b)", low)
        tax = re.search(r"\b(gst|vat|sales tax|tax|markup|mark-up|discount|tip|commission|fee)\b", low)
        if not pct and not (tax and tax.group(1) == "gst"):
            return None
        col = self.amount(mentioned)
        if col is None:
            return None
        p = float(pct.group(1)) if pct else 10.0
        if not pct:
            self.notes.append("GST taken as 10%; say another rate, e.g. 'add 15% GST'")
        label = tax.group(1).upper() if tax and tax.group(1) in ("gst", "vat") else tax.group(1) if tax else None
        if re.search(r"%\s+of\b|percent of\b", low):
            factor, name = p / 100, f"{p:g}% of {col}"
        elif re.search(r"\b(discount|minus|less|off|subtract|deduct|reduce)\b", low):
            factor, name = 1 - p / 100, f"{col} after {p:g}% {label or 'discount'}"
        else:
            factor, name = 1 + p / 100, f"{col} incl {label}" if label else f"{col} plus {p:g}%"
        name = target.group(1).strip() if target else name
        step = {"op": "compute", "column": name[:40], "expression": f"{{{col}}} * {round(factor, 6)!r}", "round": 2}
        self.numeric_new.add(step["column"])
        return [(step, f"{step['column']} = {col} x {round(factor, 6):g}")]

    def format_date(self, clause: str, low: str, mentioned: list[str]) -> Optional[list[tuple[dict, str]]]:
        cols = [c for c in mentioned if self.date_like(c)]
        if not cols and re.search(r"\bdates?\b", low):
            cols = [c for c in self.columns if self.date_like(c)]
        if not cols or not re.search(r"format|standardi|convert|normali|\bfix|iso|tidy|clean|consistent|uniform|"
                                     r"yyyy|dd|\bdates?\b", low):
            return None
        us = bool(re.search(r"\b(us|american|month first)\b|mm/dd", self.task.casefold()))
        out = "%d/%m/%Y" if re.search(r"dd/mm/yyyy", low) else "%m/%d/%Y" if re.search(r"mm/dd/yyyy", low) \
            else "%d %b %Y" if re.search(r"dd mmm yyyy", low) else "%Y-%m-%d"
        shown = out.replace("%Y", "YYYY").replace("%m", "MM").replace("%d", "DD").replace("%b", "Mon")
        steps = [({"op": "format_date", "column": col, "input_formats": US_DATE_FORMATS if us else DATE_FORMATS,
                   "output_format": out, "invalid": "keep"}, f"write every {col} date as {shown}") for col in cols]
        if not us:
            self.notes.append("dates like 03/04/2026 are read day first; say 'US dates' for month first")
        return steps

    def clean_text(self, clause: str, low: str, mentioned: list[str]) -> Optional[list[tuple[dict, str]]]:
        if not re.search(r"\b(clean|tidy|trim|whitespace|spaces|capitali[sz]\w*|title case|proper case|upper\s?case|"
                         r"lower\s?case|all caps|caps|normali[sz]e|standardi[sz]e|consistent|fix)\b", low):
            return None
        case = ("upper" if re.search(r"upper\s?case|all caps|\bcaps\b|capital letters", low) else
                "title" if re.search(r"title|capitali|proper", low) else
                "lower" if re.search(r"lower\s?case|lowercase", low) else None)
        cols = [c for c in mentioned if c in self.columns and not self.numeric(c)]
        if not cols:
            cols = [c for c in self.columns if self.info.get(c, {}).get("type") in ("text", "mixed")
                    and not self.date_like(c)]
        if not cols:
            return None
        groups: dict[str, list[str]] = {}
        for c in cols:
            if self.email_like(c):
                c_case = "lower"
            elif case:
                c_case = case
            elif "spellings" in str(self.info.get(c, {}).get("note", "")):
                c_case = "title"    # the same value written with different case: make them one
            else:
                c_case = "none"
            groups.setdefault(c_case, []).append(c)
        out = []
        for c_case, group in groups.items():
            step = {"op": "clean_text", "columns": group, "case": c_case, "collapse_spaces": True}
            out.append((step, f"trim and tidy {', '.join(group)}" + (f" ({c_case} case)" if c_case != "none" else "")))
        return out

    def fill_blank(self, clause: str, low: str, mentioned: list[str]) -> Optional[list[tuple[dict, str]]]:
        if not re.search(r"\b(fill|blank|empty|missing|default)\b", low):
            return None
        src = re.search(r"\b(?:from|using|with the value of)\s+(.+)$", clause, re.I)
        source = (_mentions(src.group(1), self.available) or [None])[0] if src else None
        val = re.search(r"\b(?:with|to|as)\s+['\"]?(.+?)['\"]?\s*$", clause, re.I)
        targets = [c for c in mentioned if c != source and (not val or c not in _mentions(val.group(1), [c]))]
        if not targets:
            return None
        out = []
        for col in targets[:4]:
            if source:
                step = {"op": "fill_blank", "column": col, "from_column": source}
                out.append((step, f"fill blank {col} from {source}"))
            elif val:
                value = _literal(val.group(1))
                if isinstance(value, (int, float)) and not self.numeric(col):
                    value = str(val.group(1)).strip(" '\"")
                step = {"op": "fill_blank", "column": col, "value": value}
                out.append((step, f"fill blank {col} with {value!r}"))
        return out or None

    def map_values(self, clause: str, low: str, mentioned: list[str]) -> Optional[list[tuple[dict, str]]]:
        if not re.search(r"\b(replace|rename|change|map|recode|convert|swap)\b", low):
            return None
        m = re.search(r"\b(?:replace|rename|change|map|recode|convert|swap)\s+(?:all\s+)?['\"]?(.+?)['\"]?\s+"
                      r"(?:with|to|->|=>|into|by)\s+['\"]?(.+?)['\"]?(?:\s+in\s+(?:the\s+)?(.+?))?\s*$", clause, re.I)
        if not m:
            return None
        old, new = m.group(1).strip(), m.group(2).strip()
        if _mentions(old, self.available) and _words(old) == _words(_mentions(old, self.available)[0]):
            return None     # "change Category to ..." names a column, not a value
        cols = _mentions(m.group(3) or "", self.available) or [
            c for c in self.columns if old.casefold() in {v.casefold() for v in self.values_of(c)}]
        if not cols:
            return None
        step = {"op": "map_values", "column": cols[0], "mapping": {old: _literal(new)}, "match": "ci"}
        return [(step, f"replace {old!r} with {new!r} in {cols[0]}")]

    RULES = ("summarize", "lookup", "dedupe", "split", "concat", "sort", "flag", "filter", "compute", "format_date",
             "clean_text", "fill_blank", "map_values")

    # ------------------------------------------------------------ plan

    @staticmethod
    def clauses(task: str) -> list[str]:
        """One clause per action; a short part with no action word ('highest first') belongs to the one before."""
        parts = [c.strip() for c in _SPLIT.split(task) if c and c.strip()]
        out: list[str] = []
        verb = re.compile(r"(?i)\b" + _VERBS + r"\b|duplicat|%")
        for part in parts:
            if out and not verb.search(part) and len(part.split()) <= 3:
                out[-1] += " " + part
            else:
                out.append(part)
        return out

    def plan(self) -> tuple[list[tuple[dict, str]], list[str]]:
        chosen, left_out = [], []
        for clause in self.clauses(self.task):
            if not re.search(r"[a-z]", clause.casefold()) or clause.casefold() in ("and", "also", "plus", "please"):
                continue
            mentioned = _mentions(clause, self.available)
            low = _without(clause, mentioned)   # trigger words never come from a column name ('Total', 'Employee')
            for rule in self.RULES:
                steps = getattr(self, rule)(clause, low, mentioned)
                if steps:
                    for step, sentence in steps:
                        chosen.append((step, sentence))
                        for col in created_columns(step):
                            if col not in self.available:
                                self.available.append(col)
                    break
            else:
                if len(re.findall(r"[a-z]+", low)) > 1:
                    left_out.append(clause)
        return chosen, left_out


def _reads(step: dict) -> list[str]:
    cols = []
    for key in ("column", "from_column", "key"):
        if isinstance(step.get(key), str):
            cols.append(step[key])
    cols += list(step.get("columns") or []) + list(step.get("group_by") or [])
    cols += [c.get("column") for c in (step.get("when") or []) + (step.get("keep_when") or []) if isinstance(c, dict)]
    cols += [b.get("column") for b in step.get("by") or [] if isinstance(b, dict)]
    cols += [v.get("column") for v in step.get("values") or [] if isinstance(v, dict)]
    if step["op"] == "compute":
        cols += expression_columns(step["expression"])
    if step["op"] == "concat":
        cols += re.findall(r"\{([^{}]+)\}", step["template"])
    return cols


def rule_plan(task: str, profile: dict, sheets: dict[str, list[str]], catalog: list[dict]) -> Optional[dict]:
    """A plan {"understanding", "assumptions", "spec", "left_out"} for the request, or None if no step fits."""
    if not sheets:
        return None
    planner = _Planner(task, profile, sheets, catalog)
    chosen, left_out = planner.plan()
    chosen = sorted(chosen, key=lambda pair: _RANK.get(pair[0]["op"], 5))[:MAX_STEPS]
    tables = {t["table"] for t in catalog}
    base = {"name": _name(task), "summary": "", "input": {"sheet": planner.sheet, "columns": [planner.columns[0]]},
            "references": planner.references, "steps": []}
    steps, sentences = [], []
    for step, sentence in chosen:
        refs = {s["reference"] for s in steps + [step] if s["op"] == "lookup"}
        trial = dict(base, steps=steps + [step], references={n: r for n, r in planner.references.items() if n in refs})
        if validate_spec(trial, sheets, catalog_tables=tables):
            left_out.append(sentence)
            continue
        steps.append(step)
        sentences.append(sentence)
    if not steps:
        return None
    used = {c for s in steps for c in _reads(s)}
    base["input"]["columns"] = [c for c in planner.columns if c in used] or [planner.columns[0]]
    referenced = {s["reference"] for s in steps if s["op"] == "lookup"}
    base["references"] = {n: r for n, r in planner.references.items() if n in referenced}
    summary = "; ".join(sentences)
    spec = dict(base, steps=steps, summary=summary[:1].upper() + summary[1:] + ".")
    if validate_spec(spec, sheets, catalog_tables=tables):
        return None
    assumptions = [f"sample mode: planned by the {ORIGIN}, not a model"] + list(dict.fromkeys(planner.notes))
    if left_out:
        assumptions.append("left out (not understood offline): " + "; ".join(f"'{c}'" for c in left_out))
    return {"understanding": f"On sheet {planner.sheet!r}: {summary}.", "assumptions": assumptions, "spec": spec,
            "left_out": left_out}


def drop_criticized(spec: dict, issues: list[str], sheets: dict[str, list[str]],
                    catalog_tables: Optional[set[str]] = None) -> Optional[tuple[dict, list[str]]]:
    """Offline revision: remove the steps the Spec Critic named ('step 3 (format_date): ...'). Returns the smaller
    spec and what was removed, or None when nothing would be left."""
    bad = {int(n) - 1 for n in re.findall(r"\bstep (\d+) \(", " ".join(issues))}
    bad_refs = set(re.findall(r"reference '([a-z0-9_]+)'", " ".join(issues)))
    keep = [s for i, s in enumerate(spec["steps"]) if i not in bad and s.get("reference") not in bad_refs]
    if not keep or len(keep) == len(spec["steps"]):
        return None
    removed = [s["op"] for s in spec["steps"] if s not in keep]
    refs = {s["reference"] for s in keep if s["op"] == "lookup"}
    smaller = dict(spec, steps=keep, references={n: r for n, r in (spec.get("references") or {}).items() if n in refs})
    used = {c for s in keep for c in _reads(s)}
    cols = sheets[spec["input"]["sheet"]]
    smaller["input"] = dict(spec["input"], columns=[c for c in cols if c in used] or [cols[0]])
    if validate_spec(smaller, sheets, catalog_tables=catalog_tables):
        return None
    return smaller, removed
