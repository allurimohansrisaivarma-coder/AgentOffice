"""Stage 3 loop tests.

The scripted generator below is a TEST DOUBLE for the model, used only in
tests to exercise the model-driven code paths (critique, revision, override)
deterministically. The application never uses it; default mode always calls
the configured provider.
"""

import json

from fastapi.testclient import TestClient

from app.adapters.meterless import ChatResult
from app.approvals import ApprovalLedger
from app.automation.compiler import compile_spec
from app.automation.spec import template_for
from app.engines.world_model import open_world
from app.pipeline import run_pipeline
from app.tracing import Tracer, verify_chain
from run import create_app

QUERY = "Which repetitive work should we automate first?"   # names no team, so every routine is a candidate


class ScriptedGenerator:
    """Replies by agent, in order. Records every prompt it was sent."""

    name = "scripted/test-double"

    def __init__(self, script: dict[str, list[str]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.prompts: list[tuple[str, str]] = []

    def chat(self, messages, *, temperature=0.2, **params):
        system, user = messages[0]["content"], messages[1]["content"]
        for key in ("PM agent", "Engineer agent", "QA agent in AgentOffice. You design",
                    "QA agent in AgentOffice reviewing", "Manager agent"):
            if key in system:
                self.prompts.append((key, user))
                return ChatResult(content=self.script[key].pop(0), model="nvidia/nemotron-test",
                                  usage={"total_tokens": 100})
        raise AssertionError(f"unexpected system prompt: {system[:60]}")


def _spec(name="invoice_matcher_v2"):
    spec = template_for("invoice_matching")
    spec["name"] = name
    return spec


def _pm_reply(saving, risk, spec, chosen="invoice_matching"):
    return "Here is my plan:\n```json\n" + json.dumps({
        "ranking": [{"routine": chosen, "estimated_hours_saved_per_week": saving, "risk": risk, "reason": "volume"},
                    {"routine": "order_status_updates", "estimated_hours_saved_per_week": 5, "risk": "medium",
                     "reason": "frequent"}],
        "chosen": chosen, "spec": spec}) + "\n```"


def _code_block(code):
    return f"<think>planning the matcher</think>\n```python\n{code}```"


def _run(tmp_path, make_settings, generator, max_revisions=3):
    settings = make_settings()
    store, pipeline, builder = open_world(settings)
    tracer = Tracer("t" + tmp_path.name[-8:], settings.log_dir, stream=None)
    result = run_pipeline(QUERY, {}, settings=settings, tracer=tracer, generator=generator, sample_mode=True,
                          store=store, pipeline=pipeline, builder=builder,
                          approvals=ApprovalLedger(settings.world_db_path.parent), max_revisions=max_revisions,
                          stages=["build"])
    events = [json.loads(line) for line in tracer.path.read_text(encoding="utf-8").splitlines()]
    return result, events, tracer.path, store


def test_model_path_critique_revision_and_rule_gate(tmp_path, make_settings):
    spec = _spec()
    buggy = compile_spec(spec).replace("<= 1.0 / 100", "< 1.0 / 200")   # tolerance halved and strict
    generator = ScriptedGenerator({
        "PM agent": [_pm_reply(60, "medium", spec),           # overclaims hours and under-rates risk
                     _pm_reply(22, "high", spec)],            # revised after the Priority Critic
        "Engineer agent": [_code_block(buggy), _code_block(compile_spec(spec))],
        "QA agent in AgentOffice. You design": [json.dumps({"cases": [
            {"record": {"invoice_id": "x", "vendor": "  vendor 0 ", "po_number": "pon-1000", "amount": 200.0},
             "why": "whitespace and lower-case PO"}]})],
        "QA agent in AgentOffice reviewing": [json.dumps({"advisories": [
            {"severity": "info", "detail": "log unmatched invoices for the AP clerk"}]})],
        "Manager agent": [json.dumps({"decision": "approve", "rationale": "Saves a day a week.",
                                      "risks": ["ERP writes"], "rollout": ["pilot"]})],
    })
    result, events, path, store = _run(tmp_path, make_settings, generator)

    collab = result["collaboration"]
    assert collab["priority_critique_rounds"] == 1
    assert collab["revision_count"] == 1
    assert [r["passed"] for r in collab["qa_rounds"]] == [False, True]
    assert "matches_spec" in collab["qa_rounds"][0]["failed_checks"]
    assert any("claims 60 h/week" in f for f in collab["evaluator_findings"])

    # The Engineer's revision prompt carried QA's concrete failing cases.
    revise_prompt = [p for k, p in generator.prompts if k == "Engineer agent"][1]
    assert "QA rejected it" in revise_prompt and "expected" in revise_prompt

    automation = result["result"]["automation"]
    assert automation["spec"]["name"] == "invoice_matcher_v2" and automation["version"] == 2
    assert automation["qa"]["model_cases"] == 1 and automation["qa"]["advisories"]
    assert result["result"]["decision"]["decision"] == "approve_for_human_review"   # rule gate, not "approve"
    assert result["approval"]["status"] == "pending"
    assert result["usage"]["model_calls"] == 7

    actions = [(e["agent_name"], e["action"], e["status"]) for e in events]
    assert ("PriorityCritic", "critique_priority", "needs_revision") in actions
    assert ("QAAgent", "critique_output", "needs_revision") in actions
    assert ("QAAgent", "critique_output", "approved") in actions
    assert ("ManagerAgent", "decision_rule_override", "overridden") in actions
    models = {e["model"] for e in events if e["action"] in ("propose_plan", "write_code", "decide")}
    assert models == {"nvidia/nemotron-test"}
    assert verify_chain(path) is None

    automation_entity = store.resolve("automation", "invoice_matcher_v2")
    assert automation_entity["attributes"]["qa_passed"] is True


def test_reasoning_with_only_a_closing_tag_is_dropped():
    from app.agents.base import extract_json, strip_reasoning
    assert strip_reasoning('The user wants JSON. {"a": 0}</think>{"a": 1}') == '{"a": 1}'
    assert extract_json('thinking about {"x": 2} first</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert strip_reasoning("<think>hidden</think>visible") == "visible"


def test_qa_failure_after_the_cap_is_rejected(tmp_path, make_settings):
    spec = _spec()
    buggy = compile_spec(spec).replace("<= 1.0 / 100", "< 1.0 / 200")
    generator = ScriptedGenerator({
        "PM agent": [_pm_reply(20, "high", spec)],
        "Engineer agent": [_code_block(buggy)] * 2,
        "QA agent in AgentOffice. You design": [json.dumps({"cases": []})],
        "Manager agent": [json.dumps({"decision": "approve_for_human_review", "rationale": "ship it"})],
    })
    result, events, _, _ = _run(tmp_path, make_settings, generator, max_revisions=1)
    assert [r["passed"] for r in result["collaboration"]["qa_rounds"]] == [False, False]
    assert result["result"]["decision"]["decision"] == "reject"
    assert result["approval"] is None
    assert result["result"]["confidence"] < 0.3


def test_invalid_model_json_is_retried(tmp_path, make_settings):
    spec = _spec()
    generator = ScriptedGenerator({
        "PM agent": ["I think invoices are the problem.", _pm_reply(20, "high", spec)],
        "Engineer agent": [_code_block(compile_spec(spec))],
        "QA agent in AgentOffice. You design": [json.dumps({"cases": []})],
        "QA agent in AgentOffice reviewing": [json.dumps({"advisories": []})],
        "Manager agent": [json.dumps({"decision": "approve_for_human_review", "rationale": "ok"})],
    })
    result, events, _, _ = _run(tmp_path, make_settings, generator)
    assert ("PMAgent", "propose_plan_rejected", "retry") in [(e["agent_name"], e["action"], e["status"])
                                                             for e in events]
    assert "no JSON object" in [e for e in events if e["action"] == "propose_plan_rejected"][0]["output_summary"]
    assert result["result"]["decision"]["decision"] == "approve_for_human_review"


def test_sample_run_over_http_and_human_approval(make_settings):
    client = TestClient(create_app(make_settings()))
    body = client.post("/run", json={"request_id": "demo-001", "user_query": QUERY,
                                     "options": {"sample_mode": True}}).json()
    assert body["status"] == "success" and body["mode"]["model"] == "offline-fallback"
    assert set(body["agents_used"]) >= {"PMAgent", "PriorityCritic", "EngineerAgent", "QAAgent", "ManagerAgent"}
    assert body["result"]["automation"]["routine"] == "invoice_matching"
    assert all(c["passed"] for c in body["result"]["automation"]["qa"]["checks"])
    trace = [json.loads(l) for l in open(body["log_path"], encoding="utf-8")]
    assert any(e["action"] == "zetaris_query" and e["status"] == "skipped" for e in trace)

    approval_id = body["approval"]["approval_id"]
    wrong = client.post(f"/approvals/{approval_id}", json={"decision": "approve", "approver": "ops lead",
                                                           "code_hash": "0" * 64})
    assert wrong.status_code == 409
    ok = client.post(f"/approvals/{approval_id}", json={"decision": "approve", "approver": "ops lead",
                                                        "code_hash": body["result"]["automation"]["code_hash"]})
    assert ok.json()["status"] == "approved" and ok.json()["approver"] == "ops lead"
    again = client.post(f"/approvals/{approval_id}", json={"decision": "reject", "approver": "x"})
    assert again.status_code == 409
    assert client.get("/approvals/apr_missing").status_code == 404

    # A second run reads the first run's facts back from the World Model.
    second = client.post("/run", json={"request_id": "demo-002", "user_query": "What should we automate next?",
                                       "options": {"sample_mode": True}}).json()
    trace = [json.loads(l) for l in open(second["log_path"], encoding="utf-8")]
    handoff = next(e for e in trace if e["action"] == "handoff" and e["target_agent"] == "PMAgent")
    assert " 0 World Model entities" not in handoff["output_summary"]
