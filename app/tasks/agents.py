"""Task-mode agents: turn "do this to my spreadsheet" into a tested, approved automation.

    FrontDesk        reads the chat message and routes it (task / analysis / reply)
    DataProfiler     profiles the uploaded sheet with Zetaris SQL (upload_cells)
    PM <-> SpecCritic  writes the step spec; the critic checks it against the real file and data
    Retriever        fetches lookup reference data (Zetaris SQL or another sheet)
    Engineer -> QA   code from the spec, tested against the spec's oracle on real and edge rows
    Manager          decision memo behind a rule gate; the human approves the exact code hash
    Operator         after approval, runs that exact code on the file and writes the output workbook
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.adapters.http import AdapterError
from app.agents.base import OFFLINE_MODEL, Agent, AgentError, extract_code
from app.approvals import sha256
from app.automation.sandbox import ALLOWED_IMPORTS, MAX_CODE_CHARS, find_secrets, policy_violations, run_batch
from app.data import catalog
from app.tasks import files, offline_planner, qa, runtime
from app.tasks.runtime import is_blank, is_num, normalize, text
from app.tasks.spec import DEMO_SPECS, demo_spec_for, describe, spec_format, validate_spec

UPLOAD_ID = re.compile(r"^(up_[0-9a-f]{12}|demo_[a-z0-9_]{1,40})$")
_PERSONAL_NAME = re.compile(r"(?i)(name|e-?mail|phone|mobile|address|birth|dob|ssn|passport|iban|account|tax)")
_EMAIL = re.compile(r"^\s*([^@\s])[^@\s]*@([^@\s]+)\s*$")


def _q(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


_DIGIT_RE = re.compile(r"[\d٠-٩۰-۹]+")
_LETTER_RE = re.compile(r"[A-Za-z\u0600-\u06FF]+")
_ARABIC_SCRIPT_RE = re.compile(r"[\u0600-\u06FF]")
_ARABIC_NORMALIZE = ("arabic_digits", "arabic_text", "arabic_separators", "arabic_names")


def _mask(col: str, value: Any) -> Any:
    """Examples shown to the model keep their shape, not their content, for personal-looking columns."""
    if value is None or is_num(value):
        return value
    s = str(value)
    m = _EMAIL.match(s)
    if m:
        lead = s[:len(s) - len(s.lstrip())]
        return f"{lead}{m.group(1)}***@{m.group(2)}{s[len(s.rstrip()):]}"
    if _PERSONAL_NAME.search(col):
        return _LETTER_RE.sub(lambda m: "x" * len(m.group(0)), _DIGIT_RE.sub(lambda m: "9" * len(m.group(0)), s))
    return s


def _shape(value: Any) -> str:
    s = text(value)
    s = _LETTER_RE.sub("A", _DIGIT_RE.sub("9", s))
    return s[:24]


def _fold_spelling(value: Any) -> str:
    """Case/spacing fold used when the profiler counts distinct spellings of one value."""
    return " ".join(text(value).split()).casefold()


def _fold_arabic(value: Any) -> str:
    """Same as _fold_spelling after Arabic digit/letter/separator normalisation."""
    return _fold_spelling(normalize(value, _ARABIC_NORMALIZE))


def _personal(col: str, values: list) -> bool:
    return bool(_PERSONAL_NAME.search(col)) or any(isinstance(v, str) and _EMAIL.match(v) for v in values[:20])


class FrontDesk(Agent):
    name = "FrontDeskAgent"
    tier = "fast"

    SYSTEM = ("You are the front desk of AgentOffice, an AI office team. Route the user's message. intents: "
              "'task' = they want something done to a spreadsheet/CSV (fill, clean, match, look up, format, flag, "
              "dedupe, sort, total...); 'analysis' = they ask where their team's time goes, what to automate, why "
              "something is slow (answered from the company's workforce data in Zetaris); 'email' = they want an "
              "email or message drafted or replied to; 'question' = a factual question about their company's "
              "people, teams, tickets, emails or time, or about the attached file (who, how many, which, list...); "
              "'reply' = anything else (greetings, questions about AgentOffice). Reply with only JSON: "
              "{\"intent\": \"task|analysis|email|question|reply\", \"reply\": <one or two friendly sentences to show "
              "the user now>}.")
    INTENTS = ("task", "analysis", "email", "question", "reply")

    TASK_WORDS = ("fill", "clean", "tidy", "match", "look up", "lookup", "format", "flag", "dedupe", "duplicate",
                  "sort", "total", "sum", "split", "merge", "convert", "standardi", "spreadsheet", "sheet", "excel",
                  "csv", "column", "row",
                  # Arabic task phrasing (UAE/Saudi users type the request in Arabic); verbs only, since
                  # short nouns like "صف" (row) also occur inside other words ("وصف", description)
                  "املأ", "املا", "عبّئ", "عبئ", "نظّف", "نظف", "طابق", "ابحث", "أصلح", "اصلح",
                  "رتّب", "رتب", "احذف")
    ANALYSIS_WORDS = ("where", "time", "hours", "automate", "bottleneck", "slow", "losing", "team", "why", "workload",
                      "أين", "اين", "وقت", "ساعات", "أتمتة", "اتمتة")
    # Asking for a message to be written, not a mention of email ("check each lead ... by email" is a task).
    EMAIL_ASK = re.compile(r"\b(draft|compose)\b|\b(write|send)\s+(an?\s+)?(e-?mail|message|note|reply)\b"
                           r"|\b(e-?mail|reply|write)\s+to\b", re.I)
    QUESTION_WORDS = ("who ", "how many", "which ", "list ", "what is", "what's", "show me", "tell me")
    # An explicit ask for an email ("Draft an email to the Finance team ...") is always an email draft,
    # whatever the fast routing model makes of the words about hours or teams that follow.
    EMAIL_DRAFT = re.compile(r"\b(draft|write|compose|prepare)\b.{0,30}\b(e-?mail|message)\b", re.I)

    def route(self, message: str, *, has_file: bool) -> dict:
        if self.offline:
            low = message.lower()
            arabic_task = any(w in message for w in self.TASK_WORDS if not w.isascii())
            if self.EMAIL_ASK.search(message):
                intent = "email"
            elif has_file or arabic_task:
                intent = "task"
            elif any(w in low or w in message for w in self.ANALYSIS_WORDS) and not any(
                    w in low or w in message for w in self.TASK_WORDS):
                intent = "analysis"
            elif any(low.startswith(w) or f" {w}" in low for w in self.QUESTION_WORDS):
                intent = "question"
            else:
                intent = "reply"
            reply = {"task": "On it: the team will plan, build and test this automation on your file, then ask you "
                             "to approve it.",
                     "analysis": "Looking into it: the team will check the workforce data in Zetaris and report "
                                 "back with evidence.",
                     "email": "Drafting it from your data; you get a draft to copy, nothing is sent.",
                     "question": "Looking it up in the data.",
                     "reply": HELP_TEXT}[intent]
            self.log("route", model=OFFLINE_MODEL, input_summary=message, output_summary={"intent": intent})
            return {"intent": intent, "reply": reply}
        obj = self.chat_json(self.SYSTEM, f"Message: {message}\nA spreadsheet is attached: {'yes' if has_file else 'no'}",
                             action="route", validate=lambda o: [] if isinstance(o, dict) and o.get("intent") in
                             self.INTENTS and isinstance(o.get("reply"), str)
                             else [f"reply must be {{\"intent\": {'|'.join(self.INTENTS)}, \"reply\": str}}"])
        if obj["intent"] != "email" and not has_file and self.EMAIL_DRAFT.search(message):
            self.log("route", status="retry", input_summary=message,
                     output_summary={"intent": "email", "corrected_from": obj["intent"],
                                     "why": "the message asks for an email to be drafted"})
            return {"intent": "email", "reply": "Drafting it from your data; you get a draft to copy, nothing is sent."}
        return {"intent": obj["intent"], "reply": obj["reply"].strip()[:600]}


HELP_TEXT = ("Hi, I'm AgentOffice. Attach a spreadsheet (CSV or Excel) and tell me what to do with it: fill in "
             "columns from the staff directory, tidy text, standardise dates, flag rows, remove duplicates, add "
             "totals. My team plans it, writes the automation, tests it on your data and asks you to approve "
             "before anything is produced. You can also ask where your team's time goes, ask a question about "
             "your company's data or your file, or have an email drafted from it (these two need the model).")


class DataProfiler(Agent):
    name = "DataProfilerAgent"

    def profile(self, upload_id: str, parsed: dict) -> dict:
        if not UPLOAD_ID.match(upload_id):
            raise AgentError(self.name, "bad_upload_id", f"invalid upload id {upload_id!r}")
        where = f"c.upload_id = {_q(upload_id)}"
        stats = self.query(
            "SELECT c.sheet_name, c.col_name, COUNT(*) AS cells, "
            "SUM(CASE WHEN c.value_type = 'blank' THEN 1 ELSE 0 END) AS blanks, "
            "SUM(CASE WHEN c.value_type = 'number' THEN 1 ELSE 0 END) AS numbers, "
            "COUNT(DISTINCT c.cell_value) AS distinct_values "
            f"FROM workforce.upload_cells c WHERE {where} GROUP BY c.sheet_name, c.col_name",
            "profile every column of the uploaded file")
        if not stats:
            raise AgentError(self.name, "upload_not_visible",
                             f"the data source has no cells for upload {upload_id}; is the API reachable from Zetaris?")
        by_col = {(r["sheet_name"], r["col_name"]): r for r in stats}
        low_card = []
        for s in parsed["sheets"]:
            for col in s["columns"]:
                r = by_col.get((s["name"], col))
                values = [row.get(col) for row in s["rows"]]
                if r and int(r["distinct_values"]) - (1 if int(r["blanks"]) else 0) <= 15 and not _personal(col, values):
                    low_card.append((s["name"], col))
        tops: dict[tuple, list] = {}
        if low_card:
            cols = ", ".join(_q(c) for c in sorted({c for _, c in low_card}))
            for r in self.query(
                    "SELECT c.sheet_name, c.col_name, c.cell_value, COUNT(*) AS n FROM workforce.upload_cells c "
                    f"WHERE {where} AND c.value_type <> 'blank' AND c.col_name IN ({cols}) "
                    "GROUP BY c.sheet_name, c.col_name, c.cell_value",
                    "exact values of the low-cardinality columns"):
                if (r["sheet_name"], r["col_name"]) in low_card:
                    tops.setdefault((r["sheet_name"], r["col_name"]), []).append((text(r["cell_value"]), int(r["n"])))

        profile = {"upload_id": upload_id, "sheets": []}
        for s in parsed["sheets"]:
            columns = []
            for col in s["columns"]:
                r = by_col.get((s["name"], col), {})
                values = [row.get(col) for row in s["rows"]]
                present = [v for v in values if not is_blank(v)]
                personal = _personal(col, values)
                info = {"name": col, "blanks": int(r.get("blanks", 0)), "numbers": int(r.get("numbers", 0)),
                        "distinct": int(r.get("distinct_values", 0)) - (1 if int(r.get("blanks", 0)) else 0),
                        "examples": [_mask(col, v) for v in present[:3]]}
                info["type"] = ("empty" if not present else "number" if info["numbers"] == len(present)
                                else "text" if info["numbers"] == 0 else "mixed")
                if personal:
                    info["personal"] = True
                if (s["name"], col) in tops:
                    pairs = sorted(tops[(s["name"], col)], key=lambda p: (-p[1], p[0]))
                    info["values"] = [f"{v!r} x{n}" for v, n in pairs]
                    folded = {_fold_spelling(v) for v, _ in pairs}
                    arabic_folded = {_fold_arabic(v) for v, _ in pairs}
                    if len(arabic_folded) < len(folded):
                        info["note"] = (f"{len(pairs)} spellings of {len(arabic_folded)} values "
                                        "(Arabic digits/letters/separators/names differ; use arabic_digits / "
                                        "arabic_text / arabic_separators / arabic_names on lookup keys)")
                    elif len(folded) < len(pairs):
                        info["note"] = f"{len(pairs)} spellings of {len(folded)} values (case/spacing differ)"
                elif info["type"] in ("text", "mixed") and present:
                    shapes: dict[str, int] = {}
                    for v in present:
                        shapes[_shape(v)] = shapes.get(_shape(v), 0) + 1
                    if 1 < len(shapes) <= 6:
                        info["shapes"] = sorted(shapes.items(), key=lambda p: -p[1])
                    notes = []
                    padded = sum(1 for v in present if isinstance(v, str) and v != v.strip())
                    if padded:
                        notes.append(f"{padded} values have leading/trailing spaces")
                    arabic_n = sum(1 for v in present if isinstance(v, str) and _ARABIC_SCRIPT_RE.search(v))
                    if arabic_n:
                        digits_n = sum(1 for v in present if isinstance(v, str)
                                       and any(ch in v for ch in "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹"))
                        sep_n = sum(1 for v in present if isinstance(v, str) and ("٫" in v or "٬" in v))
                        parts = []
                        if digits_n:
                            parts.append(f"{digits_n} with Arabic-Indic digits")
                        if sep_n:
                            parts.append(f"{sep_n} with Arabic decimal/thousands separators")
                        if arabic_n > digits_n:
                            parts.append(f"{arabic_n} with Arabic script")
                        notes.append("; ".join(parts) + " (normalize with arabic_digits / arabic_text / "
                                     "arabic_separators / arabic_names for directory lookups)")
                    if notes:
                        info["note"] = "; ".join(notes)
                columns.append(info)
            profile["sheets"].append({"name": s["name"], "rows": len(s["rows"]), "columns": columns})
        self.log("profile_file", target_agent="PMAgent", output_summary=[
            f"{s['name']}: {s['rows']} rows, {len(s['columns'])} columns" for s in profile["sheets"]],
                 data_source=self.ctx.data.name)
        return profile


class Retriever(Agent):
    name = "RetrieverAgent"

    def fetch(self, name: str, ref: dict, parsed: dict) -> list[dict]:
        if ref["source"] == "sheet":
            rows = files.sheet(parsed, ref["sheet"])["rows"]
            self.log("read_sheet", output_summary=f"reference {name}: {len(rows)} rows from sheet {ref['sheet']!r}")
            return [{f: r.get(f) for f in ref["fields"]} for r in rows]
        rows = self.query(ref["sql"], f"reference data '{name}' for a lookup")
        missing = [f for f in ref["fields"] if rows and f not in rows[0]]
        if missing:
            raise AgentError(self.name, "reference_fields", f"reference {name}: the SQL result has no field(s) "
                             f"{missing}; it returned {sorted(rows[0])}", recoverable=True)
        return [{f: r.get(f) for f in ref["fields"]} for r in rows]


def _lookup_hit_rate(rows: list[dict], column: str, ref_rows: list[dict], ref_key: str,
                     steps: list[str]) -> tuple[int, int]:
    keys = {normalize(r.get(ref_key), steps) for r in ref_rows}
    present = [normalize(r.get(column), steps) for r in rows if not is_blank(r.get(column))]
    return sum(1 for k in present if k in keys), len(present)


def _arabic_normalize_gap(steps: list[str], rows: list[dict], column: str,
                          ref_rows: list[dict], ref_key: str) -> Optional[list[str]]:
    """Arabic normalizers that would raise the lookup hit rate if added to `steps`."""
    missing = [s for s in _ARABIC_NORMALIZE if s not in steps]
    if not missing:
        return None
    hits, total = _lookup_hit_rate(rows, column, ref_rows, ref_key, steps)
    if total == 0:
        return None
    helpful = []
    for step in missing:
        alone_hits, _ = _lookup_hit_rate(rows, column, ref_rows, ref_key, list(steps) + [step])
        if alone_hits > hits:
            helpful.append(step)
    if helpful:
        return helpful
    boosted_hits, _ = _lookup_hit_rate(rows, column, ref_rows, ref_key, list(steps) + missing)
    return missing if boosted_hits > hits else None


def critique(spec: dict, parsed: dict, reference: dict) -> tuple[list[str], list[str]]:
    """The Spec Critic: (issues the PM must fix, advisories for the approver), from the real data."""
    issues, advisories = [], []
    rows = files.sheet(parsed, spec["input"]["sheet"])["rows"]
    for name, ref_rows in reference.items():
        if not ref_rows:
            issues.append(f"reference {name!r} returned 0 rows; use values and tables the catalog lists")
    for i, step in enumerate(spec["steps"]):
        where = f"step {i + 1} ({step['op']})"
        if step["op"] == "lookup" and reference.get(step["reference"]):
            steps = step.get("normalize", [])
            column = step["key"]
            ref_rows = reference[step["reference"]]
            ref_key = step["reference_key"]
            if column in files.sheet_columns(parsed)[spec["input"]["sheet"]]:
                hits, total = _lookup_hit_rate(rows, column, ref_rows, ref_key, steps)
                arabic_fix = _arabic_normalize_gap(steps, rows, column, ref_rows, ref_key)
                if total and hits == 0:
                    if arabic_fix:
                        issues.append(f"{where}: none of the {total} {column!r} values match "
                                      f"{step['reference']}.{ref_key} after normalize {steps}; keys don't match "
                                      f"because of Arabic script variants — add {arabic_fix} to normalize")
                    else:
                        issues.append(f"{where}: none of the {total} {column!r} values match "
                                      f"{step['reference']}.{ref_key} after normalize {steps}; check the "
                                      "key columns and the normalization")
                elif total and hits < total:
                    if arabic_fix:
                        issues.append(f"{where}: only {hits} of {total} {column!r} values match the reference; "
                                      f"keys don't match because of Arabic script variants — add {arabic_fix} "
                                      "to normalize")
                    else:
                        advisories.append(f"{where}: {hits} of {total} {column!r} values match the reference; "
                                          "the rest get the not_found status")
        elif step["op"] == "format_date":
            column = step["column"]
            if column in files.sheet_columns(parsed)[spec["input"]["sheet"]]:
                bad = []
                for r in rows:
                    v = r.get(column)
                    if isinstance(v, str) and v.strip():
                        if runtime.parse_date_value(v.strip(), step["input_formats"]) is None:
                            bad.append(v)
                if bad:
                    issues.append(f"{where}: {len(bad)} {column!r} values match none of the input formats "
                                  f"(Arabic months and Hijri are tried automatically), e.g. "
                                  f"{sorted(set(bad))[:3]}; add their formats (strptime codes)")
        elif step["op"] == "compute":
            for col in runtime.expression_columns(step["expression"]):
                if col in files.sheet_columns(parsed)[spec["input"]["sheet"]]:
                    texty = [r.get(col) for r in rows if isinstance(r.get(col), str) and r.get(col).strip()]
                    if texty:
                        advisories.append(f"{where}: {len(texty)} {col!r} values are text (e.g. {texty[0]!r}), so "
                                          "their result is blank")
    return issues, advisories


def _parses(value: str, fmt: str) -> bool:
    return runtime.parse_date_value(value, [fmt]) is not None


class TaskPlanner(Agent):
    name = "PMAgent"

    SYSTEM = ("You are the PM agent in AgentOffice, an AI office team that automates repetitive spreadsheet work. "
              "Turn the user's request into a precise step spec over their file, using only the step types given. "
              "Use the file profile (exact column names and values) and, for data the file lacks, the company tables "
              "in Zetaris (the catalog lists exact column values; filter values are case-sensitive). When the profile "
              "notes Arabic digits, Arabic name spellings, Latin↔Arabic name mismatches, or Arabic separators, put "
              "arabic_digits / arabic_text / arabic_separators / arabic_names on lookup and dedupe normalize lists so "
              "keys match the directory. For د.إ / ر.س / AED amounts use parse_amount before compute; format_date "
              "handles Arabic month names and Hijri. The user may write the task in Arabic. Do only "
              "what was asked. If a request cannot be expressed with the steps, do the closest useful part and say "
              "what was left out in 'assumptions'. Think briefly. Reply with only a compact JSON object.")

    def _validate(self, obj: Any, sheets: dict, approved: dict, tables: set) -> list[str]:
        if not isinstance(obj, dict):
            return ["reply must be a JSON object"]
        if obj.get("reuse"):
            if obj["reuse"] not in approved:
                return [f"reuse must be one of {sorted(approved)} or omitted"]
            return [f"cannot reuse: {e}" for e in validate_spec(approved[obj["reuse"]]["spec"], sheets,
                                                               catalog_tables=tables)]
        errors = [] if isinstance(obj.get("understanding"), str) else ["'understanding' must be a sentence"]
        return errors + [f"spec: {e}" for e in validate_spec(obj.get("spec"), sheets, catalog_tables=tables)]

    def _plan_offline(self, task: str, profile: dict, sheets: dict, approved: dict, tables: set,
                      revise_with: Optional[tuple[dict, list[str]]]) -> dict:
        """Sample mode: the built-in spec for a bundled demo file, otherwise the rule-based planner for any file."""
        if revise_with:
            previous, issues = revise_with
            smaller = offline_planner.drop_criticized(previous, issues, sheets, tables)
            if smaller is None:
                raise AgentError(self.name, "spec_rejected", "the plan does not fit the file: " + "; ".join(issues[:3]),
                                 recoverable=True)
            spec, removed = smaller
            self.log("revise_plan", model=OFFLINE_MODEL, target_agent="SpecCritic", input_summary=issues,
                     output_summary=f"dropped the step(s) the critic rejected ({', '.join(removed)}); "
                                    f"{len(spec['steps'])} left")
            return {"understanding": spec["summary"], "origin": offline_planner.ORIGIN, "spec": spec,
                    "assumptions": [f"sample mode: planned by the {offline_planner.ORIGIN}, not a model",
                                    f"dropped {', '.join(removed)}: {'; '.join(issues[:2])}"]}
        spec = demo_spec_for(sheets)
        if spec is not None:
            plan = {"understanding": spec["summary"], "spec": spec,
                    "assumptions": ["sample mode: the PM used the built-in spec for this demo file"]}
        else:
            plan = offline_planner.rule_plan(task, profile, sheets, catalog())
            if plan is None:
                raise AgentError(self.name, "no_offline_plan", "sample mode plans with simple rules, and none "
                                 "matched this request. It understands requests like: "
                                 + "; ".join(offline_planner.UNDERSTOOD[:8]) + " (use the exact column names). "
                                 "Connect Meterless Nightshift to plan anything else.", recoverable=True)
            plan["origin"] = offline_planner.ORIGIN
            spec = plan["spec"]
        reuse = next((n for n, a in approved.items() if a["spec"] == spec), None)
        if reuse:
            plan["reuse"] = reuse
        self.log("plan_task", model=OFFLINE_MODEL, input_summary=task, target_agent="SpecCritic",
                 output_summary={"reuse": reuse} if reuse else {
                     "spec": spec["name"], "steps": [s["op"] for s in spec["steps"]],
                     "origin": plan.get("origin", "built-in demo spec"), "left_out": plan.get("left_out") or "none"})
        return plan

    # "Same as last week, on this week's file: <the request>" asks for the approved automation again.
    REPEAT_PREFIX = re.compile(r"^\s*(same as|again|like|as)\b[^:]{0,60}:\s*", re.I)

    @classmethod
    def _request_key(cls, text: str) -> str:
        return " ".join(cls.REPEAT_PREFIX.sub("", text).lower().split()).rstrip(".!")

    def _same_request(self, task: str, sheets: dict, approved: dict, tables: set) -> Optional[str]:
        """The approved automation this request repeats word for word, if its spec still fits this file."""
        key = self._request_key(task)
        return next((n for n, a in approved.items() if a.get("request") and self._request_key(a["request"]) == key
                     and not validate_spec(a["spec"], sheets, catalog_tables=tables)), None)

    def plan(self, task: str, profile: dict, sheets: dict, approved: dict, lessons: list[str],
             revise_with: Optional[tuple[dict, list[str]]] = None) -> dict:
        tables = {t["table"] for t in catalog()}
        repeat = None if revise_with else self._same_request(task, sheets, approved, tables)
        if repeat:
            # A human approved this exact request before: reuse that automation (QA still re-tests it on this
            # file) instead of letting the model re-plan it into a slightly different spec that needs asking again.
            self.log("plan_task", target_agent="SpecCritic", input_summary=task,
                     output_summary={"reuse": repeat, "why": "the same request a human approved before"})
            return {"reuse": repeat, "spec": approved[repeat]["spec"],
                    "understanding": approved[repeat]["spec"]["summary"],
                    "assumptions": ["the same request as an automation you approved before"]}
        if self.offline:
            return self._plan_offline(task, profile, sheets, approved, tables, revise_with)
        workforce = [{"table": t["ref"], "rows": t["rows"], "columns": t["columns"]} for t in catalog()]
        reusable = [{"name": n, "summary": a["spec"]["summary"], "input": a["spec"]["input"],
                     "approved": a["memory"]} for n, a in approved.items()]
        # ensure_ascii=False so Nightshift/Nemotron sees real Arabic script in profile notes and examples
        # (UAE/Saudi digit and name variants), not \\uXXXX escapes.
        def _json(obj, limit: Optional[int] = None) -> str:
            text_ = json.dumps(obj, ensure_ascii=False, default=str)
            return text_ if limit is None or len(text_) <= limit else text_[:limit]

        prompt = (f"User request: {task}\n\nUploaded file profile (from Zetaris SQL over upload_cells; personal "
                  f"values masked):\n{_json(profile, 6000)}\n\nCompany tables in Zetaris "
                  f"(catalog):\n{_json(workforce, 3500)}\n\n"
                  + (f"Automations a human already approved (H-MEM); if one does exactly what is asked on this "
                     f"file, reply {{\"reuse\": <name>, \"understanding\": ...}} instead of a new spec:\n"
                     f"{_json(reusable, 2500)}\n\n" if reusable else "")
                  + ("Lessons from earlier runs (H-MEM):\n- " + "\n- ".join(lessons) + "\n\n" if lessons else "")
                  + spec_format()
                  + "\nEXAMPLE (another file):\n" + _json(DEMO_SPECS["expense_claims"])
                  + "\n\nReply with JSON: {\"understanding\": <what you will do, one sentence>, \"assumptions\": "
                    "[...], \"spec\": <spec>}")
        if revise_with:
            previous, issues = revise_with
            prompt += ("\n\nYour previous spec:\n" + _json(previous) + "\n\nThe Spec Critic checked it "
                       "against the real file and data and rejected it:\n- " + "\n- ".join(issues)
                       + "\nReply with the corrected JSON.")
        # First drafts go to the draft lane; a plan the Spec Critic rejected, or a draft that never validated,
        # escalates to the deep model.
        action = "revise_plan" if revise_with else "plan_task"

        def validate(o: Any) -> list[str]:
            return self._validate(o, sheets, approved, tables)
        try:
            obj = self.chat_json(self.SYSTEM, prompt, action=action, target_agent="SpecCritic", validate=validate,
                                 tier="deep" if revise_with else "draft")
        except AgentError as e:
            if revise_with or e.type != "invalid_model_output" or not self.ctx.lane("draft"):
                raise
            self.log("escalate", status="retry", target_agent=self.name,
                     output_summary="the draft model's plan never validated; asking the deep model")
            obj = self.chat_json(self.SYSTEM, prompt, action=action, target_agent="SpecCritic", validate=validate,
                                 tier="deep")
        if obj.get("reuse"):
            obj["spec"] = approved[obj["reuse"]]["spec"]
        obj.setdefault("assumptions", [])
        return obj


class TaskEngineer(Agent):
    name = "EngineerAgent"

    SYSTEM = ("You are the Engineer agent in AgentOffice. Write one self-contained Python 3.11 module implementing "
              "the spreadsheet automation spec exactly. Rules: define a top-level function run(rows, reference) "
              f"returning the dict described; import only from {sorted(ALLOWED_IMPORTS)}; no file, network, "
              "environment or process access; no eval, exec, getattr, global, nonlocal or dunder access; "
              "deterministic; never "
              f"special-case particular test values; stay under {MAX_CODE_CHARS} characters (implement only the "
              "step types this spec uses, no docstrings or long comments). Follow the stated semantics literally (blank rule, text(), "
              "types). Reply with only the code in one ```python block.")

    def _draft(self, code: str, version: int, origin: str) -> dict:
        return {"version": version, "code": code, "code_hash": sha256(code), "origin": origin}

    def build(self, spec: dict, lessons: list[str]) -> dict:
        if self.offline:
            draft = self._draft(qa.compile_spec(spec), 1, "compiled from the spec (offline)")
            self.log("write_code", model=OFFLINE_MODEL, target_agent="QAAgent", input_summary=spec["name"],
                     output_summary=f"v1 {draft['code_hash'][:12]}, {len(draft['code'])} chars")
            return draft
        extra = ("\n\nLessons from earlier runs (H-MEM):\n- " + "\n- ".join(lessons)) if lessons else ""
        # Code is the one job the deep model does from the start: measured live, the fast lane's code broke the
        # sandbox rules or the spec and cost more in QA rounds than it saved.
        text_ = self.chat(self.SYSTEM, describe(spec) + extra, action="write_code", target_agent="QAAgent",
                          temperature=0.1, tier="deep")
        return self._draft(extract_code(text_), 1, f"written by {self.ctx.model_name}")

    def revise(self, spec: dict, draft: dict, report: dict) -> dict:
        version = draft["version"] + 1
        if self.offline:
            self.log("revise_code", model=OFFLINE_MODEL, status="error",
                     output_summary="offline compiled code failed QA; recompiling cannot change it")
            return self._draft(qa.compile_spec(spec), version, "recompiled from the spec (offline)")
        failures = "\n".join(f"- {f['purpose']}: {f['problem']}\n  input rows: "
                             f"{json.dumps(f['input'], ensure_ascii=False, default=str)[:600]}"
                             for f in report["failures"][:6])
        problems = "\n".join(f"- {c['name']}: {c['detail']}" for c in report["checks"] if not c["passed"])
        prompt = (f"{describe(spec)}\n\nYour code (v{draft['version']}):\n```python\n{draft['code']}```\n\n"
                  f"QA rejected it.\nFailed checks:\n{problems}\n"
                  + (f"Failing cases (expected output comes from the spec's reference semantics):\n{failures}\n"
                     if failures else "")
                  + "Fix the root cause (do not special-case these inputs) and reply with the full corrected module.")
        text_ = self.chat(self.SYSTEM, prompt, action="revise_code", target_agent="QAAgent", temperature=0.1,
                          retry_count=draft["version"], tier="deep")
        return self._draft(extract_code(text_), version, f"revised by {self.ctx.model_name}")


class TaskQA(Agent):
    name = "QAAgent"
    tier = "fast"

    CASES_SYSTEM = ("You are the QA agent in AgentOffice. You design tests from the spec only; you never see the "
                    "Engineer's code. Propose tricky input rows a careless implementation would get wrong (blanks, "
                    "spaces, case, numbers stored as text, boundary values, unusual but valid values). Reply with "
                    "only JSON: {\"cases\": [{\"row\": {<input column>: value}, \"why\": \"...\"}]} with at most 6 "
                    "cases; values are strings, numbers or null.")
    REVIEW_SYSTEM = ("You are the QA agent in AgentOffice reviewing spreadsheet automation code that already passed "
                     "every automated test. List concrete robustness or maintainability concerns for the human "
                     "approver. Reply with only JSON: {\"advisories\": [{\"severity\": \"info\" | \"warning\", "
                     "\"detail\": \"...\"}]} (at most 4).")

    def __init__(self, ctx):
        super().__init__(ctx)
        self._model_cases: Optional[list[dict]] = None

    def _cases_from_model(self, spec: dict, columns: list[str], example: dict) -> list[dict]:
        if self._model_cases is not None:
            return self._model_cases
        self._model_cases = []
        if self.offline:
            self.log("design_tests", model=OFFLINE_MODEL, output_summary="spec-derived and real-row cases only")
            return self._model_cases

        def validate(o: Any) -> list[str]:
            if not isinstance(o, dict) or not isinstance(o.get("cases"), list):
                return ["reply must be {\"cases\": [...]}"]
            errs = []
            for i, c in enumerate(o["cases"][:6]):
                row = c.get("row") if isinstance(c, dict) else None
                if not isinstance(row, dict) or not row or set(row) - set(columns):
                    errs.append(f"cases[{i}].row must use only the input columns {columns}")
                elif any(v is not None and not isinstance(v, (str, int, float)) or isinstance(v, bool)
                         for v in row.values()):
                    errs.append(f"cases[{i}].row values must be strings, numbers or null")
            return errs
        try:
            obj = self.chat_json(self.CASES_SYSTEM, f"{describe(spec)}\n\nInput columns: "
                                 f"{json.dumps(columns, ensure_ascii=False)}\n"
                                 f"Example row (masked): {json.dumps(example, ensure_ascii=False, default=str)}",
                                 action="design_tests", validate=validate)
        except (AgentError, AdapterError) as e:   # optional model call: degrade, never fail the run
            self.log("design_tests", status="error", output_summary=f"model cases unusable, continuing without: "
                                                                     f"{e.message}")
            return self._model_cases
        self._model_cases = [{"row": c["row"], "why": str(c.get("why", ""))[:160]} for c in obj["cases"][:6]]
        return self._model_cases

    def design_tests(self, spec: dict, rows: list[dict]) -> list[dict]:
        """QA's own test cases from the spec alone; the pipeline runs this while the Engineer writes the code."""
        columns = list(rows[0]) if rows else spec["input"]["columns"]
        example = {c: _mask(c, rows[0].get(c)) for c in columns} if rows else {}
        return self._cases_from_model(spec, columns, example)

    def review(self, spec: dict, draft: dict, rows: list[dict], reference: dict, *, round_no: int) -> dict:
        code = draft["code"]
        checks: list[dict] = []
        failures: list[dict] = []

        def check(name: str, passed: bool, detail: str) -> bool:
            checks.append({"name": name, "passed": passed, "detail": detail})
            return passed

        violations = policy_violations(code)
        check("sandbox_policy", not violations, "; ".join(violations) or "only allowed constructs")
        secrets = find_secrets(code)
        check("no_credentials", not secrets, "; ".join(secrets) or "no hardcoded credentials")

        cases = qa.build_cases(spec, rows, reference, seed=round_no)
        columns = list(rows[0]) if rows else spec["input"]["columns"]
        for mc in self.design_tests(spec, rows):
            base = dict(rows[0]) if rows else {c: None for c in columns}
            base.update(mc["row"])
            cases.append({"purpose": f"QA model case: {mc['why']}", "kind": "model", "rows": [base],
                          "reference": reference})
        drift = qa.drift_case(spec, rows, reference) if rows else None
        preview, output_rows = None, None
        if not violations:
            calls = [[c["rows"], c["reference"]] for c in cases]
            sample_index = 0
            calls.append(calls[sample_index])     # determinism: the sample again
            if drift:
                calls.append([drift["rows"], drift["reference"]])
            batch = run_batch(code, calls, timeout_s=30.0)
            if check("runs", batch["ok"], batch.get("error") or f"{len(calls)} calls in {batch['elapsed_s']}s"):
                results = batch["results"]
                for case, got in zip(cases, results):
                    exp = qa.expected(spec, case["rows"], case["reference"])
                    problem = qa.compare(exp, got)
                    if problem:
                        failures.append({"purpose": case["purpose"], "kind": case["kind"], "problem": problem,
                                         "input": case["rows"][:3]})
                    if case["kind"] in ("full", "sample") and got.get("ok") and isinstance(got.get("output"), dict):
                        if case["kind"] == "full" or preview is None:
                            preview = got["output"]
                            output_rows = len(got["output"].get("rows") or [])
                total = len(cases)
                check("matches_spec", not failures, f"{total - len(failures)}/{total} cases match the spec's "
                                                    "expected output")
                full = next((c for c in cases if c["kind"] in ("full",)), None)
                if full is not None:
                    full_failed = any(f["kind"] == "full" for f in failures)
                    check("whole_file", not full_failed, f"all {len(full['rows'])} rows of your file match the spec"
                          if not full_failed else "the output for your whole file differs from the spec")
                again = results[len(cases)]
                check("deterministic", again == results[sample_index], "same output on a second run"
                      if again == results[sample_index] else "output changed between two identical runs")
                if drift:
                    loud = results[len(cases) + 1]
                    check("fails_loudly_on_schema_drift", not loud.get("ok") and str(loud.get("error", "")).startswith(
                        "ValueError"), f"renaming {drift['renamed']!r} raised: {loud.get('error')}" if not loud.get("ok")
                          else f"renaming {drift['renamed']!r} was silently accepted; it must raise ValueError")
        passed = all(c["passed"] for c in checks) and any(c["name"] == "matches_spec" for c in checks)
        total = len(cases)
        report = {"round": round_no, "code_hash": draft["code_hash"], "passed": passed, "checks": checks,
                  "failures": failures, "cases": total, "model_cases": len(self._model_cases or []),
                  "pass_rate": round((total - len(failures)) / total, 3) if any(
                      c["name"] == "matches_spec" for c in checks) else 0.0,
                  "advisories": [], "output_rows": output_rows,
                  "preview": {"rows": (preview or {}).get("rows", [])[:8],
                              "sheets": {k: v[:8] for k, v in ((preview or {}).get("sheets") or {}).items()}}}
        if passed and not self.offline:
            try:
                obj = self.chat_json(self.REVIEW_SYSTEM, f"Spec:\n{describe(spec)}\n\nCode:\n```python\n{code}```",
                                     action="review_code", attempts=1,
                                     validate=lambda o: [] if isinstance(o, dict) and isinstance(
                                         o.get("advisories"), list) else ["reply must be {\"advisories\": [...]}"])
                report["advisories"] = [a for a in obj["advisories"][:4] if isinstance(a, dict)]
            except (AgentError, AdapterError) as e:   # optional model call: degrade, never fail the run
                self.log("review_code", status="error", output_summary=f"advisory review skipped: {e.message}")
        matched = int(round(report["pass_rate"] * total)) if total else 0
        self.log("critique_output", target_agent="EngineerAgent", status="approved" if passed else "needs_revision",
                 confidence=report["pass_rate"], retry_count=round_no - 1,
                 input_summary=f"code v{draft['version']} {draft['code_hash'][:12]}",
                 output_summary=[f"{c['name']}: {'ok' if c['passed'] else 'FAIL - ' + c['detail']}" for c in checks],
                 cases=total, cases_matched=matched)
        return report


class TaskManager(Agent):
    name = "ManagerAgent"
    tier = "fast"

    SYSTEM = ("You are the Manager agent in AgentOffice. Write the decision memo for the human approver of a "
              "spreadsheet automation. Your decision is constrained to the allowed value given; explain it from the "
              "QA results and critic notes in plain words, name the risks, and say what the output will contain. "
              "Reply with only JSON: {\"decision\", \"rationale\", \"risks\": [...], \"what_you_get\": [...]}.")

    @staticmethod
    def confidence(report: dict, advisories: list[str]) -> float:
        if not report["passed"]:
            return round(0.3 * report["pass_rate"], 2)
        return round(min(0.98, 0.7 * report["pass_rate"] + 0.3 * (1.0 if not advisories else 0.75)), 2)

    def decide(self, task: str, plan: dict, draft: dict, report: dict, advisories: list[str],
               prior: Optional[dict]) -> dict:
        same_code = bool(prior) and prior.get("code_hash") == draft["code_hash"]
        if not report["passed"]:
            allowed = "reject"
        elif same_code and prior["status"] == "rejected":
            allowed = "escalate_to_human"
        elif same_code and prior["status"] == "approved":
            allowed = "already_approved"
        else:
            allowed = "approve_for_human_review"
        confidence = self.confidence(report, advisories)
        spec = plan["spec"]
        facts = {"request": task, "understanding": plan.get("understanding"), "assumptions": plan.get("assumptions"),
                 "steps": [s["op"] for s in spec["steps"]], "references": spec.get("references", {}),
                 "qa": {k: report[k] for k in ("passed", "pass_rate", "cases", "output_rows")}
                 | {"failed_checks": [c["name"] for c in report["checks"] if not c["passed"]],
                    "advisories": report["advisories"]},
                 "critic_advisories": advisories, "allowed_decision": allowed, "confidence": confidence,
                 "prior_approval": prior and {k: prior.get(k) for k in ("approval_id", "status", "approver")}}
        if self.offline:
            memo = {"decision": allowed,
                    "rationale": (f"The automation passed all {report['cases']} QA cases, including your whole file."
                                  if report["passed"] else f"QA still fails after the revision limit "
                                  f"({', '.join(c['name'] for c in report['checks'] if not c['passed'])}).")
                                 + (f" A human already approved this exact code ({prior['approval_id']})."
                                    if allowed == "already_approved" else ""),
                    "risks": advisories or ["none found beyond the QA advisories"],
                    "what_you_get": [f"{report['output_rows']} rows with the changes applied"]
                    + [f"sheet '{s['sheet']}'" for s in spec["steps"] if s["op"] == "summarize"]
                    + ["an AgentOffice sheet recording the approval and code hash"]}
            self.log("decide", model=OFFLINE_MODEL, output_summary=memo, confidence=confidence)
        else:
            memo = self.chat_json(self.SYSTEM, json.dumps(facts, indent=1, ensure_ascii=False, default=str),
                                  action="decide",
                                  validate=lambda o: [] if isinstance(o, dict) and isinstance(o.get("rationale"), str)
                                  and "decision" in o else ["reply must include 'decision' and 'rationale'"])
        if memo.get("decision") != allowed:
            self.log("decision_rule_override", status="overridden", output_summary=
                     f"model proposed {memo.get('decision')!r}; the rule gate allows only {allowed!r}")
            memo["decision"] = allowed
        memo["confidence"] = confidence
        return memo


class Operator(Agent):
    """Runs approved code, and only approved code, on the user's file."""
    name = "OperatorAgent"

    def execute(self, record: dict, approval: dict, code: str, parsed: dict, store: files.UploadStore) -> dict:
        if approval.get("status") != "approved":
            raise AgentError(self.name, "not_approved", f"approval {approval.get('approval_id')} is "
                             f"{approval.get('status')}; nothing runs without a human approval")
        if sha256(code) != approval["code_hash"] or approval["code_hash"] != record["code_hash"]:
            raise AgentError(self.name, "hash_mismatch", "the code on disk is not the code the human approved")
        spec = record["spec"]
        rows = files.sheet(parsed, spec["input"]["sheet"])["rows"]
        violations = policy_violations(code)
        if violations:
            raise AgentError(self.name, "policy", "; ".join(violations))
        batch = run_batch(code, [[rows, record["reference"]]], timeout_s=60.0)
        result = batch["results"][0] if batch["ok"] else batch
        if not result.get("ok"):
            raise AgentError(self.name, "run_failed", str(result.get("error")), recoverable=True)
        output = result["output"]
        columns = runtime.output_columns(spec, list(rows[0]) if rows else spec["input"]["columns"])
        provenance = {"task": record["task"], "what was done": spec["summary"],
                      "steps": [s["op"] for s in spec["steps"]], "source file": record["filename"],
                      "approved by": approval.get("approver"), "approval id": approval["approval_id"],
                      "approved at": approval.get("decided_at"), "code sha256": approval["code_hash"],
                      "QA": f"{record['qa']['cases']} cases, pass rate {record['qa']['pass_rate']}",
                      "reference data": {n: f"{len(v)} rows" for n, v in record["reference"].items()},
                      "run id": self.ctx.run_id, "mode": "sample" if self.ctx.sample_mode else "default (live)",
                      "model": record.get("model")}
        # Emit cell-write events for the Sheet pane before touching the file so the
        # workbook bytes stay identical to a run that never emitted them.
        sheet_name = spec["input"]["sheet"]
        cell_writes, changed = _emit_cell_writes(self, sheet_name, rows, output["rows"], columns,
                                                 output.get("sheets") or {}, record["reference"], spec=spec,
                                                 code_hash=approval["code_hash"])
        written = store.write_outputs(task_id=record["task_id"], filename=record["filename"], fmt=record["format"],
                                      sheet_name=sheet_name, columns=columns, rows=output["rows"],
                                      sheets=output.get("sheets") or {}, provenance=provenance)
        self.log("audit_sheet", audit_written=True, output_summary="AgentOffice audit sheet written")
        self.log("run_automation", input_summary=f"{len(rows)} rows, code {approval['code_hash'][:12]}",
                 output_summary=f"{len(output['rows'])} rows written to {', '.join(f['file_id'] for f in written)}"
                 + (f"; {changed} cells changed or added" if changed is not None else ""),
                 approval_id=approval["approval_id"], cells_changed=changed)
        return {"files": written, "rows_in": len(rows), "rows_out": len(output["rows"]), "cells_changed": changed,
                "sheets": sorted((output.get("sheets") or {}).keys()), "cell_writes": cell_writes,
                "sheet_layout": {"sheet": sheet_name, "rows": len(output["rows"])},
                "execution_run_id": self.ctx.run_id}


_CELL_WRITE_BATCH = 80
SHEET_PREVIEW_ROWS = 200        # input rows the Sheet pane holds (run.py's upload view); moved rows are drawn there
_REORDER_OPS = ("filter", "dedupe", "sort")
_ORIGIN = "\x00origin"          # row tag for _row_origins; cannot clash with a spreadsheet header


def _step_writers(spec: dict) -> tuple[dict[str, tuple[int, dict]], dict[str, tuple[int, dict]]]:
    """Map each output column / summary sheet to the (1-based step index, step) that last wrote it."""
    col_writer: dict[str, tuple[int, dict]] = {}
    sheet_writer: dict[str, tuple[int, dict]] = {}
    for i, step in enumerate(spec.get("steps") or [], start=1):
        op = step.get("op")
        if op == "clean_text":
            for c in step.get("columns") or []:
                col_writer[c] = (i, step)
        elif op in ("fill_blank", "map_values", "compute", "concat", "format_date", "flag"):
            if step.get("column"):
                col_writer[step["column"]] = (i, step)
        elif op == "split":
            for c in step.get("into") or []:
                col_writer[c] = (i, step)
        elif op == "lookup":
            for c in (step.get("fields") or {}):
                col_writer[c] = (i, step)
            if step.get("status_column"):
                col_writer[step["status_column"]] = (i, step)
        elif op == "summarize" and step.get("sheet"):
            sheet_writer[step["sheet"]] = (i, step)
    return col_writer, sheet_writer


def _lookup_source(spec: dict, step: dict) -> Optional[str]:
    ref = (spec.get("references") or {}).get(step.get("reference")) or {}
    if ref.get("source") == "zetaris":
        m = re.search(r"\bfrom\s+([\w.]+)", " ".join(str(ref.get("sql") or "").split()), flags=re.I)
        return f"Zetaris SQL on {m.group(1)}" if m else "Zetaris SQL"
    if ref.get("sheet"):
        return f"sheet {ref['sheet']}"
    return None


def _summarize_source(step: dict) -> str:
    """What the summarize step computed, from the step itself: e.g. 'sum(Amount), count(Claim ID) by Team'."""
    aggs = ", ".join(f"{v.get('agg')}({v.get('column')})" for v in step.get("values") or [])
    by = ", ".join(str(g) for g in step.get("group_by") or []) or "?"
    return f"{aggs} by {by}" if aggs else f"grouped by {by}"


_STEP_PLAIN = {
    "clean_text": "tidy text", "fill_blank": "fill blanks", "map_values": "map values",
    "lookup": "look up values", "compute": "compute", "concat": "join text", "split": "split text",
    "format_date": "fix dates", "parse_amount": "parse amounts", "flag": "flag rows", "filter": "filter rows", "dedupe": "remove duplicates",
    "sort": "sort rows", "summarize": "summarize by group",
}


def _annotate_cell(row: int, col: str, old: Any, value: Any, *, step_i: Optional[int], step: Optional[dict],
                   source: Optional[str] = None, note: Optional[str] = None, source_row: Optional[int] = None,
                   kind: str = "changed", code_hash: Optional[str] = None) -> dict:
    cell = {"row": row, "col": col, "old": old, "value": value, "kind": kind}
    if source_row is not None and source_row != row:
        cell["source_row"] = source_row
    if step_i is not None and step is not None:
        cell["step"] = step_i
        cell["step_op"] = step.get("op")
        plain = _STEP_PLAIN.get(step.get("op"), step.get("op"))
        cell["step_label"] = f"Plan step {step_i}: {plain}"
    if source:
        cell["source"] = source
    if note:
        cell["note"] = note
    if code_hash:
        cell["code_hash"] = code_hash[:12]
    return cell


def _row_origins(spec: dict, before: list[dict], after: list[dict], reference: dict) -> Optional[list[int]]:
    """The input row (0-based) behind each output row, or None when that cannot be shown.

    filter, dedupe and sort move rows, so output row j is not input row j. The reference runtime (the oracle QA
    checked the approved code against, on this whole file) is re-run with each row tagged; its lineage is trusted
    only if it reproduces the code's output exactly."""
    if not any(s.get("op") in _REORDER_OPS for s in spec.get("steps") or []):
        return list(range(len(after))) if len(before) == len(after) else None
    columns = runtime.output_columns(spec, list(before[0]) if before else spec["input"]["columns"])
    tagged = [{**row, _ORIGIN: i} for i, row in enumerate(before)]
    try:
        out = runtime.execute({**spec, "output_columns": columns + [_ORIGIN]}, tagged, reference)["rows"]
    except Exception:   # the oracle disagreeing with approved code is a lineage gap, not a reason to fail the run
        return None
    replayed = json.loads(json.dumps([{c: r.get(c) for c in columns} for r in out], default=str))
    return [r[_ORIGIN] for r in out] if replayed == after else None


def _diff_cells(before: list[dict], after: list[dict], columns: list[str], origins: Optional[list[int]], *,
                col_writer: dict, spec: dict, code_hash: Optional[str]) -> dict[tuple[int, str], dict]:
    """Each output cell whose value differs from its own input row's, keyed (row, col), plus the cells needed
    to draw the output order in the Sheet pane (rows moved or removed by filter/dedupe/sort)."""
    cells: dict[tuple[int, str], dict] = {}
    moved_by = " and ".join(_STEP_PLAIN[s["op"]] for s in spec.get("steps") or [] if s.get("op") in _REORDER_OPS)
    for j, a in enumerate(after):
        i = origins[j] if origins is not None else None
        b = before[i] if i is not None else None
        shown = before[j] if j < len(before) else {}   # what the grid shows at this position before the run
        for c in columns:
            av = a.get(c)
            step_i, step = col_writer.get(c, (None, None))
            source = _lookup_source(spec, step) if step and step.get("op") == "lookup" else None
            if b is not None and not ((is_blank(b.get(c)) and is_blank(av)) or b.get(c) == av):
                cells[(j + 1, c)] = _annotate_cell(j + 1, c, b.get(c), av, step_i=step_i, step=step, source=source,
                                                   source_row=i + 1, code_hash=code_hash)
            elif j >= SHEET_PREVIEW_ROWS or shown.get(c) == av:
                continue
            elif b is not None:
                cells[(j + 1, c)] = _annotate_cell(j + 1, c, b.get(c), av, step_i=None, step=None, kind="moved",
                                                   source_row=i + 1, code_hash=code_hash,
                                                   note=f"unchanged; row {i + 1} of your file, moved here by "
                                                        f"{moved_by}")
            else:
                cells[(j + 1, c)] = _annotate_cell(j + 1, c, None, av, step_i=step_i, step=step, source=source,
                                                   kind="written", code_hash=code_hash,
                                                   note=f"rows were moved by {moved_by}; the input row behind "
                                                        "this one could not be verified")
    return cells


def _blank_by_design(before: list[dict], after: list[dict], origins: Optional[list[int]], *, spec: dict,
                     code_hash: Optional[str]) -> dict[tuple[int, str], dict]:
    """Intentional blanks (lookup miss, blank compute input) so the Sheet pane can highlight them."""
    cells: dict[tuple[int, str], dict] = {}

    def old(j: int, c: str) -> Any:
        return before[origins[j]].get(c) if origins is not None else None

    for i, step in enumerate(spec.get("steps") or [], start=1):
        if step.get("op") == "lookup":
            status_col = step.get("status_column")
            not_found = (step.get("statuses") or {}).get("not_found", "not_found")
            source = _lookup_source(spec, step)
            for j, a in enumerate(after):
                if status_col and a.get(status_col) != not_found:
                    continue
                for c in step.get("fields") or {}:
                    if is_blank(a.get(c)):
                        cells[(j + 1, c)] = _annotate_cell(j + 1, c, old(j, c), a.get(c), step_i=i, step=step,
                                                           source=source, note=f"{not_found}, left blank by design",
                                                           kind="blank_by_design", code_hash=code_hash)
        elif step.get("op") == "compute" and step.get("column"):
            col = step["column"]
            refs = runtime.expression_columns(str(step.get("expression") or ""))
            for j, a in enumerate(after):
                if refs and is_blank(a.get(col)) and any(is_blank(a.get(c)) for c in refs):
                    cells[(j + 1, col)] = _annotate_cell(j + 1, col, old(j, col), a.get(col), step_i=i, step=step,
                                                         note="input was blank, left blank by design",
                                                         kind="blank_by_design", code_hash=code_hash)
    return cells


def _personal_columns(columns: list[str], records: list[dict]) -> set[str]:
    return {c for c in columns if _personal(c, [r.get(c) for r in records[:20]])}


def _trace_value(col: str, value: Any, personal: set[str]) -> Any:
    """Cell values in trace events keep their shape, not their content, in personal-looking columns."""
    if col in personal and value is not None and not (isinstance(value, str) and _EMAIL.match(value)):
        return re.sub(r"[A-Za-z]", "x", re.sub(r"\d", "9", text(value)))
    return _mask(col, value)


def _log_cells(agent: "Operator", sheet: str, cells: list[dict], personal: set[str], code_hash: Optional[str],
               **extra: Any) -> None:
    for i in range(0, len(cells), _CELL_WRITE_BATCH):
        batch = [{**c, "old": _trace_value(c["col"], c["old"], personal),
                  "value": _trace_value(c["col"], c["value"], personal),
                  **({"masked": True} if c["col"] in personal else {})} for c in cells[i:i + _CELL_WRITE_BATCH]]
        agent.log("cell_writes", sheet=sheet, cells=batch, code_hash=(code_hash or "")[:12],
                  input_summary=f"{len(batch)} cells on {sheet}", output_summary=f"wrote {len(batch)} cells on {sheet}",
                  **extra)


def _emit_cell_writes(agent: "Operator", sheet_name: str, before: list[dict], after: list[dict],
                      columns: list[str], extra_sheets: dict, reference: dict, *, spec: dict,
                      code_hash: Optional[str] = None) -> tuple[list[dict], Optional[int]]:
    """Log the cell changes for the Sheet pane and return (cells with real values, cells changed or added).

    The count is None when the input row behind each output row cannot be verified. Trace events mask
    personal-looking columns; the returned cells keep the real values, like the output workbook."""
    col_writer, sheet_writer = _step_writers(spec)
    origins = _row_origins(spec, before, after, reference)
    cells = _diff_cells(before, after, columns, origins, col_writer=col_writer, spec=spec, code_hash=code_hash)
    changed = sum(1 for c in cells.values() if c["kind"] == "changed") if origins is not None else None
    cells.update(_blank_by_design(before, after, origins, spec=spec, code_hash=code_hash))
    primary = list(cells.values())
    personal = _personal_columns(columns, before + after)
    _log_cells(agent, sheet_name, primary, personal, code_hash, rows_out=len(after))
    all_cells = [{"sheet": sheet_name, **c} for c in primary]
    for name, records in extra_sheets.items():
        cols = list(records[0]) if records else []
        step_i, step = sheet_writer.get(name, (None, None))
        source = _summarize_source(step) if step else None
        fresh = [_annotate_cell(r_i + 1, c, None, rec.get(c), step_i=step_i, step=step, source=source,
                                kind="added", code_hash=code_hash) for r_i, rec in enumerate(records) for c in cols]
        _log_cells(agent, name, fresh, _personal_columns(cols, records), code_hash, rows_out=len(records))
        all_cells.extend({"sheet": name, **c} for c in fresh)
    return all_cells, changed
