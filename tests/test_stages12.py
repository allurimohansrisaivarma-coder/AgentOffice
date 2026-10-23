"""Stage 1 (Discover) and stage 2 (Explain) tests, plus a full three-stage run.

ScriptedGenerator is a TEST DOUBLE for the model (see test_stage3.py); the
application never uses it.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.adapters.meterless import ChatResult
from app.agents.stage2 import check_sql, critique_claims
from app.approvals import ApprovalLedger
from app.automation.compiler import compile_spec
from app.automation.spec import template_for
from app.engines.world_model import open_world
from app.pipeline import run_pipeline
from app.tracing import Tracer, verify_chain
from run import create_app

QUERY = "Where is the finance team losing time, and what should we automate first?"
TABLES = {"people", "timesheets", "tickets", "email_events", "task_events"}

EVIDENCE = {"q1": {"rows": [{"task_category": "invoice_matching", "hours_per_week": 71.54, "people": 4},
                            {"task_category": "expense_review", "hours_per_week": 24.29, "people": 4}]}}


def cite(row, col, value):
    return {"query": "q1", "row": row, "column": col, "value": value}


def test_insight_critic_accepts_cited_numbers_and_ratios():
    claims = [{"text": "Invoice matching takes 71.5 hours a week, 2.9 times expense review (24.3 h).",
               "evidence": [cite(0, "hours_per_week", 71.54), cite(1, "hours_per_week", 24.29)]},
              {"text": "That is 295% of expense review across 4 people.",
               "evidence": [cite(0, "hours_per_week", 71.54), cite(1, "hours_per_week", 24.29),
                            cite(0, "people", 4)]}]
    assert critique_claims(claims, EVIDENCE) == []


@pytest.mark.parametrize("claim, problem", [
    ({"text": "Invoice matching takes 90 hours a week.", "evidence": [cite(0, "hours_per_week", 71.54)]},
     "number 90"),
    ({"text": "Invoice matching dominates.", "evidence": []}, "cites no evidence"),
    ({"text": "It takes 80 hours.", "evidence": [cite(0, "hours_per_week", 80)]}, "the result says 71.54"),
    ({"text": "It takes 71.54 hours.", "evidence": [cite(5, "hours_per_week", 71.54)]}, "does not point at"),
    ({"text": "It takes 71.54 hours.", "evidence": [{"query": "q9", "row": 0, "column": "x"}]}, "does not point at"),
])
def test_insight_critic_rejects_unsupported_claims(claim, problem):
    objections = critique_claims([claim], EVIDENCE)
    assert objections and problem in objections[0]


@pytest.mark.parametrize("sql, ok", [
    ("SELECT task_category FROM workforce.timesheets", True),
    ("WITH x AS (SELECT 1 AS a FROM workforce.people) SELECT a FROM x", True),
    ("DROP TABLE workforce.people", False),
    ("SELECT * FROM workforce.salaries", False),
    ("SELECT 1; SELECT 2", False),
    ("SELECT * FROM people", False),
    ("SELECT * FROM workforce.people WHERE note = 'x' UNION SELECT * FROM workforce.people; DELETE FROM x", False),
])
def test_retriever_sql_guard(sql, ok):
    assert (check_sql(sql, TABLES) == []) is ok


class ScriptedGenerator:
    name = "scripted/test-double"
    KEYS = ("Source Mapper", "Reconciler agent", "Conflict Critic", "Research Planner", "Retriever agent",
            "Report Writer", "Insight Critic", "PM agent", "Engineer agent", "QA agent in AgentOffice. You design",
            "QA agent in AgentOffice reviewing", "Manager agent")

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.prompts = []

    def chat(self, messages, *, temperature=0.2, **params):
        system, user = messages[0]["content"], messages[1]["content"]
        key = next(k for k in self.KEYS if k in system)
        self.prompts.append((key, user))
        return ChatResult(content=self.script[key].pop(0), model="nvidia/nemotron-test", usage={"total_tokens": 50})


def full_script():
    spec = template_for("invoice_matching")
    good_claims = [
        {"text": "Invoice matching takes 71.54 reported hours a week across 4 people.",
         "evidence": [cite(0, "hours_per_week", 71.54), cite(0, "people", 4)]}]
    return {
        "Source Mapper": [json.dumps({"tables": ["people", "timesheets", "tickets", "task_events"],
                                      "joins": [{"left": "timesheets.person_id", "right": "people.person_id"}],
                                      "reason": "time by person and task"})],
        "Reconciler agent": [json.dumps({"links": [
            {"task": "invoice_matching", "ticket_category": "invoice_matching", "routine": "invoice_matching",
             "confidence": 0.95},
            {"task": "order_status_updates", "ticket_category": "order_status_updates",
             "routine": "order_status_updates", "confidence": 0.9},
            {"task": "crm_data_entry", "ticket_category": None, "routine": "crm_data_entry", "confidence": 0.9}]})],
        "Conflict Critic": [json.dumps({"assessments": [
            {"task": "invoice_matching", "trusted_source": "sample:workforce.task_events",
             "explanation": "Measured hands-on time beats self-reports."}]})],
        "Research Planner": [json.dumps({"sub_questions": [
            {"question": "Which task categories take the most hours per week in Finance?"},
            {"question": "How many inbound invoice emails arrive per week?"}]})],
        "Retriever agent": [
            json.dumps({"sql": "SELECT task_category, SUM(hourz) AS hours_per_week FROM workforce.timesheets "
                               "GROUP BY task_category"}),                       # bad column -> SQL error
            json.dumps({"sql": "SELECT t.task_category, ROUND(SUM(t.hours) / 6.0, 2) AS hours_per_week, "
                               "COUNT(DISTINCT t.person_id) AS people FROM workforce.timesheets t JOIN "
                               "workforce.people p ON p.person_id = t.person_id WHERE p.team = 'Finance' "
                               "GROUP BY t.task_category ORDER BY hours_per_week DESC"}),
            json.dumps({"sql": "SELECT ROUND(COUNT(*) / 6.0, 1) AS per_week FROM workforce.email_events "
                               "WHERE subject_category = 'invoice' AND direction = 'inbound'"})],
        "Report Writer": [
            json.dumps({"headline": "Invoices eat Finance's week.", "claims": [
                {"text": "Invoice matching takes 95 hours a week.", "evidence": [cite(0, "hours_per_week", 71.54)]}]}),
            json.dumps({"headline": "Invoice matching is Finance's biggest time sink.", "claims": good_claims})],
        "Insight Critic": [json.dumps({"objections": []})],
        "PM agent": [json.dumps({"ranking": [
            {"routine": "invoice_matching", "estimated_hours_saved_per_week": 20, "risk": "high", "reason": "volume"}],
            "chosen": "invoice_matching", "spec": spec})],
        "Engineer agent": [f"```python\n{compile_spec(spec)}```"],
        "QA agent in AgentOffice. You design": [json.dumps({"cases": []})],
        "QA agent in AgentOffice reviewing": [json.dumps({"advisories": []})],
        "Manager agent": [json.dumps({"decision": "approve_for_human_review", "rationale": "QA passed."})],
    }


def test_full_three_stage_run_with_model(tmp_path, make_settings):
    settings = make_settings()
    store, pipeline, builder = open_world(settings)
    tracer = Tracer("full", settings.log_dir, stream=None)
    generator = ScriptedGenerator(full_script())
    result = run_pipeline(QUERY, {}, settings=settings, tracer=tracer, generator=generator, sample_mode=True,
                          store=store, pipeline=pipeline, builder=builder,
                          approvals=ApprovalLedger(settings.world_db_path.parent))
    events = [json.loads(l) for l in tracer.path.read_text(encoding="utf-8").splitlines()]
    actions = [(e["agent_name"], e["action"], e["status"]) for e in events]

    # Stage 1: reconciled sources -> the World Model itself surfaces the disagreement.
    task = store.resolve("task", "invoice_matching")
    assert task["attributes"]["hours_per_week"] == 32.86          # most confident source: the activity log
    assert any(c["attribute"] == "hours_per_week" for c in task["conflicts"])
    assert result["stages"]["discover"]["conflicts"]["assessments"][0]["task"] == "invoice_matching"

    # Stage 2: the Retriever fixed its own SQL; the Insight Critic rejected an unsupported number.
    assert ("RetrieverAgent", "data_query", "error") in actions
    assert result["stages"]["explain"]["evidence"][0]["attempts"] == 2
    assert ("InsightCriticAgent", "critique_claims", "needs_revision") in actions
    assert ("InsightCriticAgent", "critique_claims", "approved") in actions
    assert result["collaboration"]["insight_critique_rounds"] == 1
    assert result["result"]["findings"][0].startswith("Invoice matching takes 71.54")
    assert any("number 95" in f for f in result["collaboration"]["evaluator_findings"])

    # Stage 3 saw stage 2's verified finding.
    pm_prompt = next(p for k, p in generator.prompts if k == "PM agent")
    assert "Invoice matching takes 71.54" in pm_prompt
    assert result["result"]["decision"]["decision"] == "approve_for_human_review"
    assert len(result["agents_used"]) == 12
    assert verify_chain(tracer.path) is None
    # No person names reach any prompt: only person_ids.
    names = [r.split(",")[1] for r in open("data/sample/workforce/people.csv", encoding="utf-8").read().splitlines()[1:]]
    assert not any(name in prompt for _, prompt in generator.prompts for name in names)


def test_stage_selection(make_settings):
    client = TestClient(create_app(make_settings()))
    body = client.post("/run", json={"request_id": "d1", "user_query": QUERY,
                                     "options": {"sample_mode": True, "stages": ["discover"]}}).json()
    assert body["status"] == "success" and body["result"]["automation"] is None
    assert body["stages"]["discover"]["conflicts"]["conflicts"]
    assert body["mode"]["stages"] == ["discover"]
    bad = client.post("/run", json={"request_id": "d2", "user_query": QUERY,
                                    "options": {"sample_mode": True, "stages": ["deploy"]}})
    assert bad.status_code == 422 and bad.json()["error"]["type"] == "invalid_request"
