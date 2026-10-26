"""Stage 3, Decide & Build: a small office team that turns evidence into a tested automation.

    PM (intake + plan) <-> Priority Critic      ranks routines, writes the automation spec
    Engineer  ->  QA  ->  Engineer (revise)     code written from the spec, tested from the spec
    Manager                                     decision memo behind a rule gate + human approval

QA derives every expected result from the spec (app/automation/oracle.py),
never from the Engineer's code, so the tests cannot simply agree with it.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.adapters.http import AdapterError
from app.agents.base import OFFLINE_MODEL, Agent, AgentError, extract_code
from app.agents.stage1 import team_from_query
from app.automation import oracle
from app.automation.compiler import compile_spec
from app.automation.sandbox import ALLOWED_IMPORTS, find_secrets, policy_violations, run_in_sandbox
from app.automation.spec import TEMPLATES, describe, template_for, validate_spec
from app.approvals import sha256
from app.engines.world_model import entity_observation, relationship_observation

RISK_ORDER = ["low", "medium", "high"]
RISK_WEIGHT = {"low": 1.0, "medium": 1.5, "high": 2.0}
AUTOMATABLE_SHARE = 0.7   # offline PM's stated assumption: 70% of measured hands-on time

SPEC_FORMAT = """SPEC FORMAT (every automation is a record matcher/classifier):
{"name": snake_case, "routine": <one of the candidate routine names>, "summary": one sentence,
 "left":  {"name": snake_case, "id_field": <field>, "fields": {<field>: "str" | "number"}},
 "right": {"name": snake_case, "id_field": <field>, "fields": {<field>: "str" | "number"}},
 "key":   {"left": <str field>, "right": <str field>,
           "normalize": [any of "strip", "upper", "lower", "collapse_spaces", "digits_only"]},
 "checks": [{"left": <field>, "right": <field>, "op": "equals" | "equals_ci" | "within_pct" | "within_abs",
             "value": <number, within_* only>, "fail_status": snake_case}],
 "statuses": {"no_match": snake_case, "ambiguous": snake_case, "pass": snake_case}}
"left" = the records the routine processes (e.g. incoming documents); "right" = reference records they are
checked against. 1-5 checks, run in order, first failure decides. equals/equals_ci compare "str" fields;
within_pct/within_abs compare "number" fields. All statuses must be distinct.
EXAMPLE for a different routine:
""" + json.dumps(TEMPLATES["order_status_updates"], indent=1)


def _evidence_table(candidates: list[dict]) -> list[dict]:
    keys = ("routine", "team", "steps", "instances_per_week", "people", "active_hours_per_week", "avg_minutes_per_instance",
            "reported_hours_per_week", "ticket_logged_hours_per_week", "variant_share", "risk", "risk_reasons")
    return [{k: c[k] for k in keys} for c in candidates]


def priority_critique(proposal: dict, candidates: list[dict]) -> list[str]:
    """The Priority Critic: claims must stay within what the activity log measures."""
    by_name = {c["routine"]: c for c in candidates}
    issues = []
    scores = {}
    for item in proposal["ranking"]:
        c = by_name[item["routine"]]
        ceiling = c["active_hours_per_week"]
        claim = float(item["estimated_hours_saved_per_week"])
        if claim > ceiling * 1.05:
            reported = f"; timesheets claim {c['reported_hours_per_week']} h/week, but self-reported time is " \
                       f"not evidence of automatable work" if c["reported_hours_per_week"] else ""
            issues.append(f"{item['routine']}: claims {claim:g} h/week saved, but the activity log measures only "
                          f"{ceiling:g} h/week of hands-on time{reported}")
        if RISK_ORDER.index(item["risk"]) < RISK_ORDER.index(c["risk"]):
            issues.append(f"{item['routine']}: rated {item['risk']} risk, but the evidence says {c['risk']} "
                          f"({'; '.join(c['risk_reasons'])})")
        scores[item["routine"]] = min(claim, ceiling) / RISK_WEIGHT[c["risk"]]
    ranked = sorted(scores, key=lambda r: -scores[r])
    if proposal["chosen"] not in ranked[:2]:
        issues.append(f"chose {proposal['chosen']}, but by evidence-capped savings per unit of risk the top two are "
                      f"{', '.join(ranked[:2])}")
    return issues


class ProductManager(Agent):
    name = "PMAgent"

    SYSTEM = ("You are the PM agent in AgentOffice, a simulated office team that finds repeated manual work and "
              "builds tested automations. You do intake and prioritisation: rank the candidate routines by "
              "realistic hours saved per week against risk, choose ONE to automate first, and write its automation "
              "spec. Ground every number in the evidence given; never invent data. Reply with only a JSON object.")

    def _validate(self, obj: Any, names: set[str]) -> list[str]:
        if not isinstance(obj, dict):
            return ["reply must be a JSON object"]
        errors = []
        ranking = obj.get("ranking")
        if not isinstance(ranking, list) or not ranking:
            errors.append("'ranking' must be a non-empty list")
        else:
            for i, item in enumerate(ranking):
                if not isinstance(item, dict) or item.get("routine") not in names:
                    errors.append(f"ranking[{i}].routine must be one of {sorted(names)}")
                    continue
                v = item.get("estimated_hours_saved_per_week")
                if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                    errors.append(f"ranking[{i}].estimated_hours_saved_per_week must be a number >= 0")
                if item.get("risk") not in RISK_ORDER:
                    errors.append(f"ranking[{i}].risk must be one of {RISK_ORDER}")
        ranked = {i.get("routine") for i in ranking or [] if isinstance(i, dict)}
        if obj.get("chosen") not in ranked:
            errors.append("'chosen' must be one of the ranked routines")
        spec_errors = validate_spec(obj.get("spec"))
        errors += [f"spec: {e}" for e in spec_errors]
        if not spec_errors and obj["spec"]["routine"] != obj.get("chosen"):
            errors.append("spec.routine must equal 'chosen'")
        return errors

    def _offline(self, candidates: list[dict]) -> dict:
        ranking = []
        for c in candidates:
            saving = round(c["active_hours_per_week"] * AUTOMATABLE_SHARE, 1)
            ranking.append({"routine": c["routine"], "estimated_hours_saved_per_week": saving, "risk": c["risk"],
                            "reason": f"{c['instances_per_week']:g} runs/week x {c['avg_minutes_per_instance']:g} min "
                                      f"hands-on; assumes {AUTOMATABLE_SHARE:.0%} of that can be automated"})
        ranking.sort(key=lambda r: -r["estimated_hours_saved_per_week"] / RISK_WEIGHT[r["risk"]])
        chosen = next((r["routine"] for r in ranking if r["routine"] in TEMPLATES), None)
        if chosen is None:
            raise AgentError(self.name, "no_template", "offline mode has no spec template for any candidate")
        return {"ranking": ranking, "chosen": chosen, "spec": template_for(chosen),
                "spec_origin": "built-in template (offline)"}

    def _clamp(self, proposal: dict, candidates: list[dict]) -> dict:
        by_name = {c["routine"]: c for c in candidates}
        for item in proposal["ranking"]:
            c = by_name[item["routine"]]
            if item["estimated_hours_saved_per_week"] > c["active_hours_per_week"]:
                item["estimated_hours_saved_per_week"] = c["active_hours_per_week"]
                item["reason"] = f"{item.get('reason', '')} [capped at measured hands-on time by the Priority Critic]"
            if RISK_ORDER.index(item["risk"]) < RISK_ORDER.index(c["risk"]):
                item["risk"] = c["risk"]
        return proposal

    def plan(self, query: str, evidence: dict, context: dict, *, blocked: Optional[dict] = None,
             max_rounds: int = 2) -> dict:
        candidates = evidence["routines"]
        if not candidates:
            raise AgentError(self.name, "no_candidates", "no repeated routines were found in the activity log")
        team = team_from_query(query, sorted({c["team"] for c in candidates}))
        scoped = [c for c in candidates if c["team"] == team]
        if scoped:
            candidates = scoped
        # H-MEM: a routine a human already approved or rejected is not re-proposed while alternatives exist.
        blocked = blocked or {}
        held = [c for c in candidates if c["routine"] in blocked]
        if held and len(held) < len(candidates):
            candidates = [c for c in candidates if c["routine"] not in blocked]
            for c in held:
                self.log("memory_applied", target_agent="PriorityCritic", status="applied",
                         output_summary=f"not re-proposing {c['routine']}: H-MEM records that "
                                        f"{blocked[c['routine']]['content']}")
        elif held:
            self.log("memory_applied", status="no_alternative", output_summary=
                     "every candidate already has a human decision on record; keeping them for the Manager to weigh")
        names = {c["routine"] for c in candidates}
        self.log("intake", input_summary=query, output_summary=(f"scoped to the {team} team: " if scoped else "")
                 + f"{len(candidates)} candidate routines: "
                 + ", ".join(f"{c['routine']} ({c['active_hours_per_week']:g} h/week hands-on)" for c in candidates))

        prompt = (f"Question from the office team: {query}\n\nEvidence over {evidence['weeks']:g} weeks (computed from "
                  f"the activity log, timesheets and ticket system):\n{json.dumps(_evidence_table(candidates), indent=1)}"
                  f"\n\nRelevant context from the shared World Model:\n{json.dumps(context, default=str)[:3000]}"
                  f"\n\n{SPEC_FORMAT}\n\nReply with JSON: {{\"ranking\": [{{\"routine\", "
                  f"\"estimated_hours_saved_per_week\", \"risk\": \"low|medium|high\", \"reason\"}}], "
                  f"\"chosen\": <routine>, \"spec\": <spec for the chosen routine>}}")
        if self.offline:
            proposal = self._offline(candidates)
            self.log("propose_plan", model=OFFLINE_MODEL, target_agent="PriorityCritic",
                     output_summary={"chosen": proposal["chosen"], "ranking": proposal["ranking"]})
        else:
            proposal = self.chat_json(self.SYSTEM, prompt, action="propose_plan", target_agent="PriorityCritic",
                                      validate=lambda o: self._validate(o, names))
            proposal["spec_origin"] = f"written by {self.ctx.model_name}"

        rounds = 0
        critiques: list[list[str]] = []
        if "PriorityCritic" not in self.ctx.agents_used:
            self.ctx.agents_used.append("PriorityCritic")
        while True:
            issues = priority_critique(proposal, candidates)
            critiques.append(issues)
            self.ctx.tracer.event("PriorityCritic", "critique_priority", target_agent=self.name,
                                  status="needs_revision" if issues else "approved",
                                  input_summary={"chosen": proposal["chosen"], "ranking": proposal["ranking"]},
                                  output_summary=issues or "every claim is within the measured evidence",
                                  retry_count=rounds)
            if not issues:
                break
            if rounds >= max_rounds or self.offline:
                proposal = self._clamp(proposal, candidates)
                self.log("priority_clamped", status="escalated", output_summary=
                         "claims capped at measured hands-on time and evidence risk after the revision limit")
                break
            rounds += 1
            revise = (prompt + "\n\nYour previous reply:\n" + json.dumps(proposal, default=str)
                      + "\n\nThe Priority Critic rejected it:\n- " + "\n- ".join(issues)
                      + "\nRevise the ranking (and choice/spec if needed) so every claim is within the evidence.")
            proposal = self.chat_json(self.SYSTEM, revise, action="revise_plan", target_agent="PriorityCritic",
                                      validate=lambda o: self._validate(o, names))
            proposal["spec_origin"] = f"written by {self.ctx.model_name} (revision {rounds})"

        self._record(candidates, proposal, query)
        return {**proposal, "priority_rounds": rounds, "priority_critiques": critiques}

    def _record(self, candidates: list[dict], proposal: dict, query: str) -> None:
        by_rank = {item["routine"]: (i + 1, item) for i, item in enumerate(proposal["ranking"])}
        obs = []
        ctx = [("run", self.ctx.run_id)]
        for c in candidates:
            obs.append(entity_observation(
                "routine", c["routine"], source=self.data_source("workforce.task_events", c["evidence_rows"]),
                attributes={k: c[k] for k in ("instances_per_week", "people", "active_hours_per_week",
                                              "reported_hours_per_week", "ticket_logged_hours_per_week",
                                              "variant_share", "risk", "steps")},
                contexts=ctx))
            if c["routine"] in by_rank:
                rank, item = by_rank[c["routine"]]
                name = f"automate {c['routine']}"
                obs.append(entity_observation(
                    "automation_candidate", name, source=self.source(), contexts=ctx,
                    attributes={"rank": rank, "estimated_hours_saved_per_week": item["estimated_hours_saved_per_week"],
                                "risk": item["risk"], "chosen": c["routine"] == proposal["chosen"]}))
                obs.append(relationship_observation(("automation_candidate", name), "targets", ("routine", c["routine"]),
                                                    source=self.source(), contexts=ctx))
        self.remember(obs, "ranked automation candidates")


class Engineer(Agent):
    name = "EngineerAgent"

    SYSTEM = ("You are the Engineer agent in AgentOffice. Write one self-contained Python 3.11 module that implements "
              "the automation spec exactly. Rules: define a top-level function run(left, right) returning the list "
              f"described; import only from {sorted(ALLOWED_IMPORTS)}; no file, network, environment or process "
              "access; no eval, exec, getattr or dunder access; deterministic; raise ValueError naming the field when "
              "a record lacks a declared field; never special-case particular test values. Reply with only the code "
              "in one ```python block.")

    def _draft(self, code: str, version: int, origin: str) -> dict:
        return {"version": version, "code": code, "code_hash": sha256(code), "origin": origin}

    @staticmethod
    def _lessons(lessons: Optional[dict]) -> str:
        if not lessons or not lessons.get("memories"):
            return ""
        return "\n\nLessons from earlier runs (H-MEM):\n" + "\n".join(f"- {m['content']}" for m in lessons["memories"])

    def build(self, spec: dict, lessons: Optional[dict] = None) -> dict:
        self._lesson_text = self._lessons(lessons)
        if self.offline:
            draft = self._draft(compile_spec(spec), 1, "compiled from the spec (offline)")
            self.log("write_code", model=OFFLINE_MODEL, target_agent="QAAgent", input_summary=spec["name"],
                     output_summary=f"v1 {draft['code_hash'][:12]}, {len(draft['code'])} chars")
            return draft
        text = self.chat(self.SYSTEM, f"{describe(spec)}\n\nSpec JSON:\n{json.dumps(spec, indent=1)}"
                         + self._lesson_text, action="write_code", target_agent="QAAgent", temperature=0.1)
        return self._draft(extract_code(text), 1, f"written by {self.ctx.model_name}")

    def revise(self, spec: dict, draft: dict, report: dict) -> dict:
        version = draft["version"] + 1
        if self.offline:
            # The compiler is deterministic, so a failing offline draft means a compiler bug: say so.
            self.log("revise_code", model=OFFLINE_MODEL, status="error",
                     output_summary="offline compiler output failed QA; recompiling cannot change it")
            return self._draft(compile_spec(spec), version, "recompiled from the spec (offline)")
        failures ="\n".join(f"- case {f['left_id']} ({f['purpose']}): input {json.dumps(f['input'])} -> expected "
                             f"{json.dumps(f['expected'])}, got {json.dumps(f['got'])}" for f in report["failures"][:8])
        problems = "\n".join(f"- {c['name']}: {c['detail']}" for c in report["checks"] if not c["passed"])
        prompt = (f"{describe(spec)}\n\nSpec JSON:\n{json.dumps(spec, indent=1)}\n\nYour code (v{draft['version']}):\n"
                  f"```python\n{draft['code']}```\n\nQA rejected it.\nFailed checks:\n{problems}\n"
                  + (f"Failing cases:\n{failures}\n" if failures else "")
                  + "Fix the root cause (do not special-case these inputs) and reply with the full corrected module."
                  + getattr(self, "_lesson_text", ""))
        text = self.chat(self.SYSTEM, prompt, action="revise_code", target_agent="QAAgent", temperature=0.1,
                         retry_count=draft["version"])
        return self._draft(extract_code(text), version, f"revised by {self.ctx.model_name}")


class QAAgent(Agent):
    name = "QAAgent"

    CASES_SYSTEM = ("You are the QA agent in AgentOffice. You design tests from the spec only; you never see the "
                    "Engineer's code. Propose tricky LEFT records that a careless implementation would get wrong "
                    "(boundaries, whitespace and case, unusual but valid values). Reply with only JSON: "
                    "{\"cases\": [{\"record\": {<every left field>}, \"why\": \"...\"}]} with at most 6 cases.")
    REVIEW_SYSTEM = ("You are the QA agent in AgentOffice reviewing code that already passed every automated test. "
                     "List concrete robustness or maintainability concerns for a human approver. Reply with only JSON: "
                     "{\"advisories\": [{\"severity\": \"info\" | \"warning\", \"detail\": \"...\"}]} (at most 5).")

    def __init__(self, ctx):
        super().__init__(ctx)
        self._extra_cases: Optional[list[dict]] = None

    def _validate_cases(self, obj: Any, spec: dict) -> list[str]:
        if not isinstance(obj, dict) or not isinstance(obj.get("cases"), list):
            return ["reply must be {\"cases\": [...]}"]
        fields = spec["left"]["fields"]
        errors = []
        for i, case in enumerate(obj["cases"][:6]):
            record = case.get("record") if isinstance(case, dict) else None
            if not isinstance(record, dict) or set(record) != set(fields):
                errors.append(f"cases[{i}].record must have exactly the fields {sorted(fields)}")
                continue
            for name, ftype in fields.items():
                value = record[name]
                ok = isinstance(value, str) if ftype == "str" else \
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                if not ok:
                    errors.append(f"cases[{i}].record.{name} must be a {ftype}")
        return errors

    def _model_cases(self, spec: dict) -> list[dict]:
        if self._extra_cases is not None:
            return self._extra_cases
        self._extra_cases = []
        if self.offline:
            self.log("design_tests", model=OFFLINE_MODEL, output_summary="deterministic spec-derived cases only")
            return self._extra_cases
        try:
            obj = self.chat_json(self.CASES_SYSTEM, f"{describe(spec)}\n\nSpec JSON:\n{json.dumps(spec, indent=1)}",
                                 action="design_tests", validate=lambda o: self._validate_cases(o, spec))
        except (AgentError, AdapterError) as e:   # optional model call: degrade, never fail the run
            self.log("design_tests", status="error", output_summary=f"model cases unusable, continuing without: "
                                                                     f"{e.message}")
            return self._extra_cases
        for i, case in enumerate(obj["cases"][:6]):
            record = dict(case["record"])
            record[spec["left"]["id_field"]] = f"Q{i + 1:03d}"
            self._extra_cases.append({"record": record, "why": str(case.get("why", ""))[:200]})
        return self._extra_cases

    def review(self, spec: dict, draft: dict, *, round_no: int) -> dict:
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

        cases = oracle.generate_cases(spec, seed=round_no)
        extra = self._model_cases(spec)
        left = cases["left"] + [c["record"] for c in extra]
        purpose = {**cases["purpose"], **{c["record"][spec["left"]["id_field"]]: f"QA model case: {c['why']}"
                                          for c in extra}}
        expected = oracle.expected_output(spec, left, cases["right"])

        if not violations:
            result = run_in_sandbox(code, left, cases["right"])
            if check("runs", result["ok"], result.get("error") or f"ran in {result['elapsed_s']}s"):
                output = result["output"]
                shape_ok = (isinstance(output, list) and len(output) == len(left) and all(
                    isinstance(r, dict) and set(r) == {"left_id", "right_id", "status"} for r in output))
                if check("output_shape", shape_ok, "one {left_id, right_id, status} per left record, in order"
                         if shape_ok else "output is not one {left_id, right_id, status} object per left record"):
                    by_id = {rec[spec["left"]["id_field"]]: rec for rec in left}
                    for exp, got in zip(expected, output):
                        if exp != got:
                            failures.append({"left_id": exp["left_id"], "purpose": purpose.get(exp["left_id"], ""),
                                             "input": by_id[exp["left_id"]], "expected": exp, "got": got})
                    check("matches_spec", not failures, f"{len(left) - len(failures)}/{len(left)} cases match the "
                                                        "spec's expected output")
                    again = run_in_sandbox(code, left, cases["right"])
                    check("deterministic", again.get("output") == output, "same output on a second run"
                          if again.get("output") == output else "output changed between two identical runs")
                    drifted = oracle.drift_copy(spec, cases)
                    loud = run_in_sandbox(code, drifted["left"], drifted["right"])
                    check("fails_loudly_on_schema_drift", not loud["ok"],
                          f"renaming '{drifted['renamed']}' raised: {loud.get('error')}" if not loud["ok"] else
                          f"renaming '{drifted['renamed']}' was silently accepted; it must raise ValueError")

        passed = all(c["passed"] for c in checks) and any(c["name"] == "matches_spec" for c in checks)
        total = len(left)
        report = {"round": round_no, "code_hash": draft["code_hash"], "passed": passed, "checks": checks,
                  "failures": failures, "cases": total, "model_cases": len(extra),
                  "pass_rate": round((total - len(failures)) / total, 3) if any(
                      c["name"] == "matches_spec" for c in checks) else 0.0,
                  "advisories": []}
        if passed and not self.offline:
            try:
                obj = self.chat_json(self.REVIEW_SYSTEM, f"Spec:\n{describe(spec)}\n\nCode:\n```python\n{code}```",
                                     action="review_code", attempts=1,
                                     validate=lambda o: [] if isinstance(o, dict) and isinstance(
                                         o.get("advisories"), list) else ["reply must be {\"advisories\": [...]}"])
                report["advisories"] = [a for a in obj["advisories"][:5] if isinstance(a, dict)]
            except (AgentError, AdapterError) as e:   # optional model call: degrade, never fail the run
                self.log("review_code", status="error", output_summary=f"advisory review skipped: {e.message}")
        matched = int(round(report["pass_rate"] * total)) if total else 0
        self.log("critique_output", target_agent="EngineerAgent", status="approved" if passed else "needs_revision",
                 confidence=report["pass_rate"], retry_count=round_no - 1,
                 input_summary=f"code v{draft['version']} {draft['code_hash'][:12]}",
                 output_summary=[f"{c['name']}: {'ok' if c['passed'] else 'FAIL - ' + c['detail']}" for c in checks],
                 cases=total, cases_matched=matched)
        return report


class Manager(Agent):
    name = "ManagerAgent"

    SYSTEM = ("You are the Manager agent in AgentOffice. Write the decision memo for a human approver. Your decision "
              "is constrained to the allowed value given; explain it from the evidence and QA results, name the "
              "risks, and give rollout steps. Reply with only JSON: {\"decision\", \"rationale\", \"risks\": [...], "
              "\"rollout\": [...]}.")

    @staticmethod
    def confidence(report: dict, candidate: dict) -> float:
        if not report["passed"]:
            return round(0.3 * report["pass_rate"], 2)
        evidence = min(1.0, candidate["instances_per_week"] / 50)
        risk = {"low": 1.0, "medium": 0.7, "high": 0.4}[candidate["risk"]]
        return round(0.45 * report["pass_rate"] + 0.2 * evidence + 0.15 * (1 - candidate["variant_share"])
                     + 0.2 * risk, 2)

    def decide(self, query: str, plan: dict, candidate: dict, draft: dict, report: dict, history: list[dict],
               prior: Optional[dict] = None) -> dict:
        same_code = bool(prior) and prior["facts"].get("code_hash") == draft["code_hash"]
        if not report["passed"]:
            allowed = "reject"
        elif same_code and prior["facts"]["decision"] == "rejected":
            allowed = "escalate_to_human"   # never re-ask a human to approve code they already rejected
        elif same_code:
            allowed = "already_approved"
        else:
            allowed = "approve_for_human_review"
        confidence = self.confidence(report, candidate)
        chosen = next(i for i in plan["ranking"] if i["routine"] == plan["chosen"])
        facts = {"question": query, "routine": candidate["routine"], "steps": candidate["steps"],
                 "evidence": {k: candidate[k] for k in ("instances_per_week", "people", "active_hours_per_week",
                                                        "reported_hours_per_week", "ticket_logged_hours_per_week",
                                                        "variant_share", "risk", "risk_reasons")},
                 "estimated_hours_saved_per_week": chosen["estimated_hours_saved_per_week"],
                 "qa": {"passed": report["passed"], "pass_rate": report["pass_rate"], "cases": report["cases"],
                        "rounds": len(history), "failed_checks": [c["name"] for c in report["checks"] if not c["passed"]],
                        "advisories": report["advisories"]},
                 "allowed_decision": allowed, "confidence": confidence,
                 "prior_human_decision": prior["content"] if prior else None,
                 "prior_decision_is_for_this_exact_code": same_code}
        if self.offline:
            memory = ""
            if prior:
                memory = (f" H-MEM: {prior['content']}" + (" This is the same code, so it is not sent for approval "
                                                            "again." if same_code else ""))
            memo = {"decision": allowed,
                    "rationale": (f"{candidate['routine']} runs {candidate['instances_per_week']:g} times a week "
                                  f"({candidate['active_hours_per_week']:g} h/week hands-on). The automation "
                                  + (f"passed all {report['cases']} QA cases after {len(history)} round(s)."
                                     if report["passed"] else f"still fails QA after {len(history)} round(s).")
                                  + memory),
                    "risks": candidate["risk_reasons"] or ["low-risk routine"],
                    "rollout": ["dry run on one week of real records and compare with manual results",
                                "enable for one team member, keep manual review of non-pass statuses"]}
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
        memo["confidence_formula"] = ("0.45*QA pass rate + 0.2*evidence volume + 0.15*routine consistency + "
                                      "0.2*(1 - risk)" if report["passed"] else "0.3*QA pass rate (QA failed)")
        return memo
