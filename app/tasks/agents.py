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

from app.agents.base import OFFLINE_MODEL, Agent, AgentError, extract_code
from app.approvals import sha256
from app.automation.sandbox import ALLOWED_IMPORTS, find_secrets, policy_violations, run_batch
from app.data import catalog
from app.tasks import files, qa, runtime
from app.tasks.runtime import is_blank, is_num, normalize, text
from app.tasks.spec import DEMO_SPECS, demo_spec_for, describe, spec_format, validate_spec

UPLOAD_ID = re.compile(r"^(up_[0-9a-f]{12}|demo_[a-z0-9_]{1,40})$")
_PERSONAL_NAME = re.compile(r"(?i)(name|e-?mail|phone|mobile|address|birth|dob|ssn|passport|iban|account|tax)")
_EMAIL = re.compile(r"^\s*([^@\s])[^@\s]*@([^@\s]+)\s*$")


def _q(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


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
        return re.sub(r"[A-Za-z]", "x", re.sub(r"\d", "9", s))
    return s


def _shape(value: Any) -> str:
    s = text(value)
    s = re.sub(r"[A-Za-z]+", "A", re.sub(r"\d+", "9", s))
    return s[:24]


def _personal(col: str, values: list) -> bool:
    return bool(_PERSONAL_NAME.search(col)) or any(isinstance(v, str) and _EMAIL.match(v) for v in values[:20])


class FrontDesk(Agent):
    name = "FrontDeskAgent"

    SYSTEM = ("You are the front desk of AgentOffice, an AI office team. Route the user's message. intents: "
              "'task' = they want something done to a spreadsheet/CSV (fill, clean, match, look up, format, flag, "
              "dedupe, sort, total...); 'analysis' = they ask where their team's time goes, what to automate, why "
              "something is slow (answered from the company's workforce data in Zetaris); 'reply' = anything else "
              "(greetings, questions about AgentOffice). Reply with only JSON: {\"intent\": \"task|analysis|reply\", "
              "\"reply\": <one or two friendly sentences to show the user now>}.")

    TASK_WORDS = ("fill", "clean", "tidy", "match", "look up", "lookup", "format", "flag", "dedupe", "duplicate",
                  "sort", "total", "sum", "split", "merge", "convert", "standardi", "spreadsheet", "sheet", "excel",
                  "csv", "column", "row")
    ANALYSIS_WORDS = ("where", "time", "hours", "automate", "bottleneck", "slow", "losing", "team", "why", "workload")

    def route(self, message: str, *, has_file: bool) -> dict:
        if self.offline:
            low = message.lower()
            if has_file:
                intent = "task"
            elif any(w in low for w in self.ANALYSIS_WORDS) and not any(w in low for w in self.TASK_WORDS):
                intent = "analysis"
            else:
                intent = "reply"
            reply = {"task": "On it: the team will plan, build and test this automation on your file, then ask you "
                             "to approve it.",
                     "analysis": "Looking into it: the team will check the workforce data in Zetaris and report "
                                 "back with evidence.",
                     "reply": HELP_TEXT}[intent]
            self.log("route", model=OFFLINE_MODEL, input_summary=message, output_summary={"intent": intent})
            return {"intent": intent, "reply": reply}
        obj = self.chat_json(self.SYSTEM, f"Message: {message}\nA spreadsheet is attached: {'yes' if has_file else 'no'}",
                             action="route", validate=lambda o: [] if isinstance(o, dict) and o.get("intent") in (
                                 "task", "analysis", "reply") and isinstance(o.get("reply"), str)
                             else ["reply must be {\"intent\": task|analysis|reply, \"reply\": str}"])
        return {"intent": obj["intent"], "reply": obj["reply"].strip()[:600]}


HELP_TEXT = ("Hi, I'm AgentOffice. Attach a spreadsheet (CSV or Excel) and tell me what to do with it: fill in "
             "columns from the staff directory, tidy text, standardise dates, flag rows, remove duplicates, add "
             "totals. My team plans it, writes the automation, tests it on your data and asks you to approve "
             "before anything is produced. You can also ask where your team's time goes.")


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
                    folded = {" ".join(v.split()).casefold() for v, _ in pairs}
                    if len(folded) < len(pairs):
                        info["note"] = f"{len(pairs)} spellings of {len(folded)} values (case/spacing differ)"
                elif info["type"] in ("text", "mixed") and present:
                    shapes: dict[str, int] = {}
                    for v in present:
                        shapes[_shape(v)] = shapes.get(_shape(v), 0) + 1
                    if 1 < len(shapes) <= 6:
                        info["shapes"] = sorted(shapes.items(), key=lambda p: -p[1])
                    padded = sum(1 for v in present if isinstance(v, str) and v != v.strip())
                    if padded:
                        info["note"] = f"{padded} values have leading/trailing spaces"
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
            keys = {normalize(r.get(step["reference_key"]), steps) for r in reference[step["reference"]]}
            column = step["key"]
            if column in files.sheet_columns(parsed)[spec["input"]["sheet"]]:
                present = [normalize(r.get(column), steps) for r in rows if not is_blank(r.get(column))]
                hits = sum(1 for k in present if k in keys)
                if present and hits == 0:
                    issues.append(f"{where}: none of the {len(present)} {column!r} values match "
                                  f"{step['reference']}.{step['reference_key']} after normalize {steps}; check the "
                                  "key columns and the normalization")
                elif present and hits < len(present):
                    advisories.append(f"{where}: {hits} of {len(present)} {column!r} values match the reference; "
                                      "the rest get the not_found status")
        elif step["op"] == "format_date":
            column = step["column"]
            if column in files.sheet_columns(parsed)[spec["input"]["sheet"]]:
                bad = []
                for r in rows:
                    v = r.get(column)
                    if isinstance(v, str) and v.strip():
                        if not any(_parses(v.strip(), f) for f in step["input_formats"]):
                            bad.append(v)
                if bad:
                    issues.append(f"{where}: {len(bad)} {column!r} values match none of the input formats, e.g. "
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
    from datetime import datetime
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


class TaskPlanner(Agent):
    name = "PMAgent"

    SYSTEM = ("You are the PM agent in AgentOffice, an AI office team that automates repetitive spreadsheet work. "
              "Turn the user's request into a precise step spec over their file, using only the step types given. "
              "Use the file profile (exact column names and values) and, for data the file lacks, the company tables "
              "in Zetaris (the catalog lists exact column values; filter values are case-sensitive). Do only what "
              "was asked. If a request cannot be expressed with the steps, do the closest useful part and say what "
              "was left out in 'assumptions'. Think briefly. Reply with only a compact JSON object.")

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

    def plan(self, task: str, profile: dict, sheets: dict, approved: dict, lessons: list[str],
             revise_with: Optional[tuple[dict, list[str]]] = None) -> dict:
        tables = {t["table"] for t in catalog()}
        if self.offline:
            spec = demo_spec_for(sheets)
            reuse = next((n for n, a in approved.items() if spec and a["spec"] == spec), None)
            if spec is None:
                raise AgentError(self.name, "no_offline_plan", "sample mode can plan only the bundled demo files "
                                 f"({', '.join(DEMO_SPECS)}); connect Meterless Nightshift to plan your own tasks",
                                 recoverable=True)
            plan = {"understanding": spec["summary"], "assumptions": ["sample mode: the PM used the built-in spec "
                                                                      "for this demo file"], "spec": spec}
            if reuse:
                plan["reuse"] = reuse
            self.log("plan_task", model=OFFLINE_MODEL, input_summary=task, target_agent="SpecCritic",
                     output_summary={"reuse": reuse} if reuse else {"spec": spec["name"], "steps": len(spec["steps"])})
            return plan
        workforce = [{"table": t["ref"], "rows": t["rows"], "columns": t["columns"]} for t in catalog()]
        reusable = [{"name": n, "summary": a["spec"]["summary"], "input": a["spec"]["input"],
                     "approved": a["memory"]} for n, a in approved.items()]
        prompt = (f"User request: {task}\n\nUploaded file profile (from Zetaris SQL over upload_cells; personal "
                  f"values masked):\n{json.dumps(profile, default=str)[:6000]}\n\nCompany tables in Zetaris "
                  f"(catalog):\n{json.dumps(workforce)[:3500]}\n\n"
                  + (f"Automations a human already approved (H-MEM); if one does exactly what is asked on this "
                     f"file, reply {{\"reuse\": <name>, \"understanding\": ...}} instead of a new spec:\n"
                     f"{json.dumps(reusable)[:2500]}\n\n" if reusable else "")
                  + (f"Lessons from earlier runs (H-MEM):\n- " + "\n- ".join(lessons) + "\n\n" if lessons else "")
                  + spec_format()
                  + "\nEXAMPLE (another file):\n" + json.dumps(DEMO_SPECS["expense_claims"])
                  + "\n\nReply with JSON: {\"understanding\": <what you will do, one sentence>, \"assumptions\": "
                    "[...], \"spec\": <spec>}")
        if revise_with:
            previous, issues = revise_with
            prompt += ("\n\nYour previous spec:\n" + json.dumps(previous) + "\n\nThe Spec Critic checked it "
                       "against the real file and data and rejected it:\n- " + "\n- ".join(issues)
                       + "\nReply with the corrected JSON.")
        obj = self.chat_json(self.SYSTEM, prompt, action="revise_plan" if revise_with else "plan_task",
                             target_agent="SpecCritic", validate=lambda o: self._validate(o, sheets, approved, tables))
        if obj.get("reuse"):
            obj["spec"] = approved[obj["reuse"]]["spec"]
        obj.setdefault("assumptions", [])
        return obj


class TaskEngineer(Agent):
    name = "EngineerAgent"

    SYSTEM = ("You are the Engineer agent in AgentOffice. Write one self-contained Python 3.11 module implementing "
              "the spreadsheet automation spec exactly. Rules: define a top-level function run(rows, reference) "
              f"returning the dict described; import only from {sorted(ALLOWED_IMPORTS)}; no file, network, "
              "environment or process access; no eval, exec, getattr or dunder access; deterministic; never "
              "special-case particular test values. Follow the stated semantics literally (blank rule, text(), "
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
        text_ = self.chat(self.SYSTEM, describe(spec) + extra, action="write_code", target_agent="QAAgent",
                          temperature=0.1)
        return self._draft(extract_code(text_), 1, f"written by {self.ctx.model_name}")

    def revise(self, spec: dict, draft: dict, report: dict) -> dict:
        version = draft["version"] + 1
        if self.offline:
            self.log("revise_code", model=OFFLINE_MODEL, status="error",
                     output_summary="offline compiled code failed QA; recompiling cannot change it")
            return self._draft(qa.compile_spec(spec), version, "recompiled from the spec (offline)")
        failures = "\n".join(f"- {f['purpose']}: {f['problem']}\n  input rows: {json.dumps(f['input'], default=str)[:600]}"
                             for f in report["failures"][:6])
        problems = "\n".join(f"- {c['name']}: {c['detail']}" for c in report["checks"] if not c["passed"])
        prompt = (f"{describe(spec)}\n\nYour code (v{draft['version']}):\n```python\n{draft['code']}```\n\n"
                  f"QA rejected it.\nFailed checks:\n{problems}\n"
                  + (f"Failing cases (expected output comes from the spec's reference semantics):\n{failures}\n"
                     if failures else "")
                  + "Fix the root cause (do not special-case these inputs) and reply with the full corrected module.")
        text_ = self.chat(self.SYSTEM, prompt, action="revise_code", target_agent="QAAgent", temperature=0.1,
                          retry_count=draft["version"])
        return self._draft(extract_code(text_), version, f"revised by {self.ctx.model_name}")


class TaskQA(Agent):
    name = "QAAgent"

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
            obj = self.chat_json(self.CASES_SYSTEM, f"{describe(spec)}\n\nInput columns: {json.dumps(columns)}\n"
                                 f"Example row (masked): {json.dumps(example, default=str)}",
                                 action="design_tests", validate=validate)
        except AgentError as e:
            self.log("design_tests", status="error", output_summary=f"model cases unusable, continuing without: "
                                                                     f"{e.message}")
            return self._model_cases
        self._model_cases = [{"row": c["row"], "why": str(c.get("why", ""))[:160]} for c in obj["cases"][:6]]
        return self._model_cases

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
        example = {c: _mask(c, rows[0].get(c)) for c in columns} if rows else {}
        for mc in self._cases_from_model(spec, columns, example):
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
            except AgentError as e:
                self.log("review_code", status="error", output_summary=f"advisory review skipped: {e.message}")
        self.log("critique_output", target_agent="EngineerAgent", status="approved" if passed else "needs_revision",
                 confidence=report["pass_rate"], retry_count=round_no - 1,
                 input_summary=f"code v{draft['version']} {draft['code_hash'][:12]}",
                 output_summary=[f"{c['name']}: {'ok' if c['passed'] else 'FAIL - ' + c['detail']}" for c in checks])
        return report


class TaskManager(Agent):
    name = "ManagerAgent"

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
            memo = self.chat_json(self.SYSTEM, json.dumps(facts, indent=1, default=str), action="decide",
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
        written = store.write_outputs(task_id=record["task_id"], filename=record["filename"], fmt=record["format"],
                                      sheet_name=spec["input"]["sheet"], columns=columns, rows=output["rows"],
                                      sheets=output.get("sheets") or {}, provenance=provenance)
        changed = _changed_cells(rows, output["rows"], columns) if len(rows) == len(output["rows"]) else None
        self.log("run_automation", input_summary=f"{len(rows)} rows, code {approval['code_hash'][:12]}",
                 output_summary=f"{len(output['rows'])} rows written to {', '.join(f['file_id'] for f in written)}"
                 + (f"; {changed} cells changed or added" if changed is not None else ""),
                 approval_id=approval["approval_id"])
        return {"files": written, "rows_in": len(rows), "rows_out": len(output["rows"]), "cells_changed": changed,
                "sheets": sorted((output.get("sheets") or {}).keys())}


def _changed_cells(before: list[dict], after: list[dict], columns: list[str]) -> int:
    return sum(1 for b, a in zip(before, after) for c in columns
               if not (is_blank(b.get(c)) and is_blank(a.get(c))) and b.get(c) != a.get(c))
