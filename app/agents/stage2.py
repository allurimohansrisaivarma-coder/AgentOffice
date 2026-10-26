"""Stage 2, Explain (Track 2): why is this the bottleneck? Evidence-backed, or it does not ship.

    Research Planner  splits the question into sub-questions the data can answer
    Retriever         writes one SQL query per sub-question and runs it; SQL errors go back
                      to the model to fix (a bounded tool-use loop)
    Report Writer     drafts claims, citing the exact result cell behind every number
    Insight Critic    checks every citation against the real rows, rejects unsupported
                      numbers or claims, and sends objections back for revision (capped)
"""

from __future__ import annotations

import json
import re
from itertools import permutations
from typing import Any, Optional

from app.agents.base import OFFLINE_MODEL, Agent, AgentError
from app.agents.stage1 import team_from_query
from app.adapters.http import AdapterError
from app.adapters.zetaris import is_read_only, split_statements
from app.data import PORTABLE_SQL_NOTE
from app.engines.world_model import entity_observation

MAX_ROWS = 30
_WRITE_WORDS = re.compile(r"\b(create|drop|insert|update|delete|alter|grant|upsert|cache|truncate|merge|attach)\b",
                          re.I)
_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def check_sql(sql: str, tables: set[str]) -> list[str]:
    statements = split_statements(sql)
    if len(statements) != 1:
        return [f"exactly one statement is allowed, got {len(statements)}"]
    statement = statements[0]
    errors = []
    if not is_read_only(statement) or not statement.lstrip().lower().startswith(("select", "with")):
        errors.append("only SELECT (or WITH ... SELECT) queries are allowed")
    if _WRITE_WORDS.search(statement):
        errors.append("the query must not contain write or DDL keywords")
    referenced = set(re.findall(r"\bworkforce\.([a-z_]+)", statement, re.I))
    unknown = referenced - tables
    if unknown:
        errors.append(f"unknown tables: {sorted(unknown)}; available: {sorted(tables)}")
    if not referenced:
        errors.append("reference tables as workforce.<table>")
    return errors


class ResearchPlanner(Agent):
    name = "ResearchPlannerAgent"

    SYSTEM = ("You are the Research Planner agent in AgentOffice. Break the question into 2 to 4 sub-questions that "
              "can each be answered by ONE SQL query over the catalog, and that together explain where time goes and "
              "why. Reply with only JSON: {\"sub_questions\": [{\"id\": \"q1\", \"question\": \"...\"}]}.")

    def plan(self, query: str, catalog: list[dict], conflicts: dict) -> list[dict]:
        def validate(obj: Any) -> list[str]:
            if not isinstance(obj, dict) or not isinstance(obj.get("sub_questions"), list):
                return ["reply must be {\"sub_questions\": [...]}"]
            subs = obj["sub_questions"]
            if not 2 <= len(subs) <= 4:
                return ["give 2 to 4 sub-questions"]
            if any(not isinstance(s, dict) or not str(s.get("question", "")).strip() for s in subs):
                return ["every sub-question needs a non-empty 'question'"]
            return []

        if self.offline:
            team = team_from_query(query, ["Finance", "Operations", "Sales", "Admin"])
            scope = f"the {team} team" if team else "each team"
            subs = [{"id": "q1", "question": f"Which task categories take the most reported hours per week for {scope}?"},
                    {"id": "q2", "question": "How much hands-on time per week does each repeated case-based routine "
                                             "take in the activity log?"},
                    {"id": "q3", "question": "How many inbound emails with attachments arrive per week, by subject "
                                             "category, that start work?"}]
            self.log("plan_research", model=OFFLINE_MODEL, output_summary=subs, target_agent="RetrieverAgent")
            return subs
        obj = self.chat_json(self.SYSTEM, f"Question: {query}\n\nCatalog:\n{json.dumps(catalog, indent=1)}\n\n"
                             f"Known data-quality conflicts:\n{json.dumps(conflicts.get('assessments', []))}",
                             action="plan_research", validate=validate, target_agent="RetrieverAgent")
        return [{"id": f"q{i + 1}", "question": str(s["question"]).strip()} for i, s in enumerate(obj["sub_questions"])]


OFFLINE_SQL = {
    "q1": ("SELECT t.task_category, ROUND(SUM(t.hours) / 6.0, 2) AS hours_per_week, "
           "COUNT(DISTINCT t.person_id) AS people FROM workforce.timesheets t JOIN workforce.people p "
           "ON p.person_id = t.person_id {team_filter}GROUP BY t.task_category ORDER BY hours_per_week DESC LIMIT 8"),
    "q2": ("SELECT app, action, COUNT(*) AS events, ROUND(SUM(duration_seconds) / 3600.0 / 6.0, 2) AS "
           "hours_per_week FROM workforce.task_events WHERE case_ref IS NOT NULL AND case_ref <> '' "
           "GROUP BY app, action ORDER BY hours_per_week DESC LIMIT 10"),
    "q3": ("SELECT subject_category, counterparty, ROUND(COUNT(*) / 6.0, 1) AS per_week FROM workforce.email_events "
           "WHERE direction = 'inbound' AND has_attachment = 'true' GROUP BY subject_category, counterparty "
           "ORDER BY per_week DESC LIMIT 8"),
}


class Retriever(Agent):
    name = "RetrieverAgent"

    SYSTEM = ("You are the Retriever agent in AgentOffice. Write ONE read-only SQL query that answers the "
              "sub-question, using only the catalog's tables as workforce.<table>. " + PORTABLE_SQL_NOTE +
              " The data covers 6 working weeks; divide totals by 6.0 for per-week figures and give columns clear "
              "aliases. Where the catalog lists a column's values, filter with those exact values (matching is "
              "case-sensitive); never guess a value. Return at most 30 rows. Think briefly. Reply with only JSON: "
              "{\"sql\": \"...\"}.")

    def _fetch(self, sql: str, purpose: str) -> tuple[list[dict], bool]:
        """(at most MAX_ROWS rows, whether the result had more): one extra row tells a full result from a cut one."""
        rows = self.query(sql, purpose, limit=MAX_ROWS + 1)
        return rows[:MAX_ROWS], len(rows) > MAX_ROWS

    def retrieve(self, query: str, sub_questions: list[dict], catalog: list[dict], *, attempts: int = 3) -> list[dict]:
        tables = {t["table"] for t in catalog}
        team = team_from_query(query, ["Finance", "Operations", "Sales", "Admin"])
        evidence = []
        for sub in sub_questions:
            if self.offline:
                if sub["id"] not in OFFLINE_SQL:
                    continue
                sql = OFFLINE_SQL[sub["id"]].format(team_filter=f"WHERE p.team = '{team}' " if team else "")
                rows, truncated = self._fetch(sql, sub["question"])
                evidence.append({**sub, "sql": sql, "rows": rows, "truncated": truncated, "attempts": 1})
                continue
            prompt = f"Sub-question: {sub['question']}\n\nCatalog:\n{json.dumps(catalog, indent=1)}"
            for attempt in range(attempts):
                obj = self.chat_json(self.SYSTEM, prompt, action="write_sql" if attempt == 0 else "fix_sql",
                                     validate=lambda o: check_sql(o.get("sql", ""), tables) if isinstance(o, dict)
                                     else ["reply must be {\"sql\": \"...\"}"])
                sql = split_statements(obj["sql"])[0]
                try:
                    rows, truncated = self._fetch(sql, sub["question"])
                except AdapterError as e:
                    if attempt + 1 == attempts:
                        self.log("give_up_sub_question", status="error", output_summary=f"{sub['id']}: {e.message}")
                        break
                    prompt += (f"\n\nYour query:\n{sql}\nfailed with: {e.message}\n"
                               "Fix it (portable SQL only) and reply with the corrected JSON.")
                    continue
                if not rows and attempt + 1 < attempts:
                    # Empty results are almost always a filter value that does not exist in the data.
                    self.log("empty_result", status="retry", output_summary=f"{sub['id']}: 0 rows, asking for a fix")
                    prompt += (f"\n\nYour query:\n{sql}\nreturned 0 rows. A filter value probably does not exist; "
                               "use only values the catalog lists, exactly as written, and reply with the corrected JSON.")
                    continue
                evidence.append({**sub, "sql": sql, "rows": rows, "truncated": truncated, "attempts": attempt + 1})
                break
        if not evidence:
            raise AgentError(self.name, "no_evidence", "no sub-question could be answered from the data")
        return evidence


def _cited_numbers(claim: dict, evidence: dict[str, dict]) -> list[float]:
    values = []
    for c in claim["evidence"]:
        value = evidence[c["query"]]["rows"][c["row"]][c["column"]]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return values


def _supported(number: float, decimals: int, cited: list[float]) -> bool:
    if number.is_integer() and abs(number) <= 10:
        return True   # small counts and ordinals ("2 people", "3 sources")
    candidates = list(cited) + [a / b for a, b in permutations(cited, 2) if b] + \
        [100 * a / b for a, b in permutations(cited, 2) if b]
    return any(round(v, decimals) == round(number, decimals) for v in candidates)


def critique_claims(claims: list[dict], evidence: dict[str, dict]) -> list[str]:
    """The Insight Critic's deterministic checks. Each objection names the claim index."""
    objections = []
    for i, claim in enumerate(claims):
        citations = claim.get("evidence") if isinstance(claim, dict) else None
        if not isinstance(claim, dict) or not str(claim.get("text", "")).strip():
            objections.append(f"claim {i}: needs non-empty text")
            continue
        if not isinstance(citations, list) or not citations:
            objections.append(f"claim {i}: cites no evidence")
            continue
        bad = False
        for c in citations:
            q, row, col = (c.get("query"), c.get("row"), c.get("column")) if isinstance(c, dict) else (None,) * 3
            rows = evidence.get(q, {}).get("rows", [])
            if not isinstance(row, int) or not 0 <= row < len(rows) or col not in rows[row]:
                objections.append(f"claim {i}: citation {c} does not point at a cell of a returned result")
                bad = True
                continue
            actual = rows[row][col]
            stated = c.get("value", actual)
            if isinstance(actual, (int, float)) and not isinstance(actual, bool):
                if not isinstance(stated, (int, float)) or abs(float(stated) - float(actual)) > max(
                        0.01, 0.01 * abs(float(actual))):
                    objections.append(f"claim {i}: cites {q} row {row} {col} = {stated}, but the result says {actual}")
                    bad = True
            elif str(stated) != str(actual):
                objections.append(f"claim {i}: cites {q} row {row} {col} = {stated!r}, but the result says {actual!r}")
                bad = True
        if bad:
            continue
        cited = _cited_numbers(claim, evidence)
        for raw in _NUMBER.findall(claim["text"]):
            text = raw.replace(",", "")
            decimals = len(text.split(".")[1]) if "." in text else 0
            if not _supported(float(text), decimals, cited):
                objections.append(f"claim {i}: the number {raw} is not one of its cited values (or a ratio of them)")
    return objections


class ReportWriter(Agent):
    name = "ReportWriterAgent"

    SYSTEM = ("You are the Report Writer agent in AgentOffice. Explain where the team's time goes and why, using "
              "ONLY the query results given. Every claim must cite the cells it relies on as {\"query\": <id>, "
              "\"row\": <0-based row index>, \"column\": <column>, \"value\": <the cell value>}, and every number in "
              "a claim's text must be one of its cited values (rounded) or a ratio/percentage of two of them. "
              "Reply with only JSON: {\"headline\": \"...\", \"claims\": [{\"text\": \"...\", \"evidence\": [...]}]} "
              "with 3 to 6 claims.")

    def _offline(self, evidence: list[dict]) -> dict:
        claims = []
        by_id = {e["id"]: e for e in evidence}
        if by_id.get("q1", {}).get("rows"):
            top = by_id["q1"]["rows"][0]
            claims.append({"text": f"{top['task_category']} takes the most reported time: "
                                   f"{top['hours_per_week']:g} hours per week across {top['people']} people.",
                           "evidence": [{"query": "q1", "row": 0, "column": "task_category",
                                         "value": top["task_category"]},
                                        {"query": "q1", "row": 0, "column": "hours_per_week",
                                         "value": top["hours_per_week"]},
                                        {"query": "q1", "row": 0, "column": "people", "value": top["people"]}]})
        if by_id.get("q2", {}).get("rows"):
            rows = by_id["q2"]["rows"][:2]
            for i, r in enumerate(rows):
                claims.append({"text": f"In the activity log, {r['app']} {r['action']} alone takes "
                                       f"{r['hours_per_week']:g} hands-on hours per week.",
                               "evidence": [{"query": "q2", "row": i, "column": c, "value": r[c]}
                                            for c in ("app", "action", "hours_per_week")]})
        if by_id.get("q3", {}).get("rows"):
            r = by_id["q3"]["rows"][0]
            claims.append({"text": f"The work is triggered by about {r['per_week']:g} inbound {r['subject_category']} "
                                   f"emails with attachments per week from {r['counterparty']}.",
                           "evidence": [{"query": "q3", "row": 0, "column": c, "value": r[c]}
                                        for c in ("per_week", "subject_category", "counterparty")]})
        headline = claims[0]["text"] if claims else "No evidence was returned."
        return {"headline": headline, "claims": claims}

    def write(self, query: str, evidence: list[dict], conflicts: dict, objections: Optional[list[str]] = None,
              previous: Optional[dict] = None) -> dict:
        if self.offline:
            draft = self._offline(evidence)
            self.log("write_report", model=OFFLINE_MODEL, output_summary=draft, target_agent="InsightCriticAgent")
            return draft
        payload = {"question": query, "results": [{"query": e["id"], "sub_question": e["question"], "sql": e["sql"],
                                                   "rows": e["rows"], "truncated_at_30_rows": e.get("truncated", False)}
                                                  for e in evidence],
                   "source_conflicts": conflicts.get("assessments", [])}
        prompt = json.dumps(payload, indent=1, default=str)
        if objections:
            prompt += ("\n\nYour previous draft:\n" + json.dumps(previous, default=str) + "\n\nThe Insight Critic "
                       "objected:\n- " + "\n- ".join(objections) + "\nFix or drop those claims.")
        return self.chat_json(self.SYSTEM, prompt, action="revise_report" if objections else "write_report",
                              target_agent="InsightCriticAgent",
                              validate=lambda o: [] if isinstance(o, dict) and isinstance(o.get("claims"), list)
                              and o["claims"] else ["reply must have a non-empty 'claims' list"])


class InsightCritic(Agent):
    name = "InsightCriticAgent"

    SYSTEM = ("You are the Insight Critic agent in AgentOffice. The numbers in these claims have already been checked "
              "against the data. Object only to wording that overstates its evidence (causal claims the data cannot "
              "show, generalising beyond the rows). Reply with only JSON: {\"objections\": [{\"claim\": <index>, "
              "\"reason\": \"...\"}]} (empty list if none).")

    def review(self, draft: dict, evidence: list[dict], round_no: int) -> list[str]:
        by_id = {e["id"]: e for e in evidence}
        objections = critique_claims(draft["claims"], by_id)
        if not objections and not self.offline:
            try:
                obj = self.chat_json(self.SYSTEM, json.dumps(draft["claims"], indent=1, default=str),
                                     action="review_wording", attempts=1,
                                     validate=lambda o: [] if isinstance(o, dict) and isinstance(
                                         o.get("objections"), list) else ["reply must be {\"objections\": [...]}"])
                objections = [f"claim {o.get('claim')}: {o.get('reason')}" for o in obj["objections"][:5]
                              if isinstance(o, dict)]
            except (AgentError, AdapterError) as e:   # optional model call: degrade, never fail the run
                self.log("review_wording", status="error", output_summary=f"wording review skipped: {e.message}")
        self.log("critique_claims", target_agent="ReportWriterAgent", retry_count=round_no - 1,
                 status="needs_revision" if objections else "approved",
                 input_summary=[c.get("text") for c in draft["claims"] if isinstance(c, dict)],
                 output_summary=objections or "every number traces to a cited cell")
        return objections


def explain(ctx, query: str, catalog: list[dict], conflicts: dict, *, max_rounds: int = 2) -> dict:
    planner, retriever, writer, critic = ResearchPlanner(ctx), Retriever(ctx), ReportWriter(ctx), InsightCritic(ctx)
    subs = planner.plan(query, catalog, conflicts)
    evidence = retriever.retrieve(query, subs, catalog)
    draft = writer.write(query, evidence, conflicts)
    rounds, history = 0, []
    while True:
        objections = critic.review(draft, evidence, rounds + 1)
        history.append(objections)
        if not objections:
            break
        if rounds >= max_rounds or writer.offline:
            break
        rounds += 1
        draft = writer.write(query, evidence, conflicts, objections, draft)
    by_id = {e["id"]: e for e in evidence}
    final_objections = critique_claims(draft["claims"], by_id)
    bad = {int(m.group(1)) for o in final_objections for m in [re.match(r"claim (\d+):", o)] if m}
    verified = [c for i, c in enumerate(draft["claims"]) if i not in bad]
    dropped = [c for i, c in enumerate(draft["claims"]) if i in bad]
    if dropped:
        critic.log("drop_claims", status="escalated", output_summary=f"{len(dropped)} claim(s) dropped: still "
                                                                     "unsupported after the revision limit")
    critic.remember([entity_observation("finding", c["text"][:120], source=critic.source(),
                                        attributes={"evidence": c["evidence"]}, contexts=[("run", ctx.run_id)])
                     for c in verified], "verified findings")
    return {"sub_questions": subs, "evidence": [{k: e.get(k) for k in ("id", "question", "sql", "rows", "truncated",
                                                                       "attempts")} for e in evidence],
            "headline": draft.get("headline", ""), "claims": verified, "dropped_claims": dropped,
            "critique_rounds": rounds, "objections": history}
