import json
import math

import pytest
from fastapi.testclient import TestClient

from app.adapters.meterless import ChatResult
from app.approvals import ApprovalLedger
from app.automation.compiler import compile_spec
from app.automation.spec import template_for
from app.engines.hmem import MemoryRetrievalService, MemoryStoreService, TrustLedgerService, new_record, open_memory
from app.engines.hmem.mining import direct_capture, validate_candidates
from app.engines.hmem.retrieval import WEIGHTS
from app.engines.world_model import open_world
from app.pipeline import run_pipeline
from app.tracing import Tracer
from run import create_app

AGENT = {"kind": "agent", "name": "MemoryKeeperAgent"}
HUMAN = {"kind": "human", "name": "ops lead"}


@pytest.fixture
def mem(tmp_path):
    ledger = TrustLedgerService(tmp_path / "ledger.jsonl")
    store = MemoryStoreService(":memory:", ledger)
    return store, MemoryRetrievalService(store), ledger


def lesson(content, **kw):
    kw.setdefault("derived_from", ["run1:qa_round_1"])
    kw.setdefault("domain", "invoice_matching")
    return new_record(content, type=kw.pop("type", "factual"), source=kw.pop("source", AGENT),
                      confidence=kw.pop("confidence", 0.8), **kw)


# ------------------------------------------------------------------ records + approval boundary

def test_records_need_a_source_and_valid_fields():
    with pytest.raises(ValueError, match="no source"):
        new_record("x", type="factual", source={}, confidence=0.8)
    with pytest.raises(ValueError, match="type"):
        new_record("x", type="procedural", source=AGENT, confidence=0.8)
    a = lesson("Use <= for tolerance boundaries")
    assert a["id"] == lesson("use <= for  tolerance boundaries")["id"]
    assert a["layer"] == "short_term" and new_record("x", type="preference", source=HUMAN,
                                                     confidence=0.9)["layer"] == "long_term"
    for field in ("id", "content", "type", "layer", "timestamp", "lastAccessed", "accessCount", "tags",
                  "embedding", "confidence", "source", "provenance", "domain", "namespace", "entities",
                  "relatedTo", "supersedes", "supersededBy", "derivedFrom", "goalRunId"):
        assert field in a


@pytest.mark.parametrize("record, reason", [
    (lambda: lesson("no evidence here", derived_from=[]), "no provenance"),
    (lambda: lesson("call ana at ana.dune@example.com"), "personal data"),
    (lambda: lesson("phone 555-123-4567 for vendor"), "personal data"),
    (lambda: lesson("weak hunch", confidence=0.3), "below 0.5"),
    (lambda: lesson("about Ana", type="personal"), "personal memories"),
])
def test_approval_boundary_rejects(mem, record, reason):
    store, _, ledger = mem
    outcome = store.propose(record(), actor="test")
    assert outcome["status"] == "rejected" and reason in outcome["reason"]
    assert store.all() == [] and ledger.entries()[-1]["action"] == "proposal_rejected"


def test_dates_are_not_mistaken_for_phone_numbers(mem):
    store, _, _ = mem
    assert store.propose(lesson("Run on 2026-10-23 found 120.5 invoices/week"), actor="t")["status"] == "created"


def test_reinforcement_promotes_layers(mem):
    store, _, ledger = mem
    first = store.propose(lesson("Size savings by hands-on time"), actor="t")["memory"]
    second = store.propose(lesson("Size savings by hands-on time", derived_from=["run2:x"]), actor="t")
    assert second["status"] == "reinforced"
    m = second["memory"]
    assert m["layer"] == "working" and m["confidence"] == round(first["confidence"] + 0.05, 3)
    assert m["derivedFrom"] == ["run1:qa_round_1", "run2:x"]
    assert store.propose(lesson("Size savings by hands-on time"), actor="t")["memory"]["layer"] == "long_term"
    assert [e["action"] for e in ledger.entries()][-4:] == ["feedback", "promote", "feedback", "promote"]


# ------------------------------------------------------------------ conflicts

def decision(verb, note=""):
    return new_record(f"ops lead {verb} the invoice_po_matcher automation for invoice_matching (code abc){note}",
                      type="preference", source=HUMAN, confidence=0.95, domain="invoice_matching",
                      entities=["routine:invoice_matching", "automation:invoice_po_matcher"], derived_from=["apr_1"],
                      facts={"kind": "approval_decision", "key": "approval:invoice_matching", "decision": verb})


def test_newer_human_decision_supersedes_and_nothing_is_deleted(mem):
    store, _, ledger = mem
    old = store.propose(decision("approved"), actor="human")["memory"]
    outcome = store.propose(decision("rejected", ": ERP writes need sign-off"), actor="human")
    (conflict,) = outcome["conflicts"]
    assert conflict["decision_confidence"] >= 0.7 and "opposing phrases" in conflict["reasons"]
    loser = store.get(old["id"])
    assert loser["supersededBy"] == outcome["memory"]["id"]          # kept, penalized, not deleted
    assert store.get(outcome["memory"]["id"])["supersedes"] == old["id"]
    assert [m["id"] for m in store.all()] == [outcome["memory"]["id"]]
    resolved = [e for e in ledger.entries() if e["action"] == "conflict_resolved"][0]
    assert resolved["previousState"] and resolved["newState"]


def test_unclear_conflicts_go_to_human_review(mem):
    store, _, ledger = mem
    a = store.propose(lesson("invoice matching takes 30 hours per week of hands-on work"), actor="t")["memory"]
    b = store.propose(lesson("invoice matching takes 45 hours per week of hands-on work",
                             derived_from=["run2:y"]), actor="t")
    assert b["conflicts"] and b["conflicts"][0]["decision_confidence"] < 0.7
    assert "review" in store.get(a["id"])["tags"] and "review" in store.get(b["memory"]["id"])["tags"]
    assert store.get(a["id"])["supersededBy"] is None


# ------------------------------------------------------------------ retrieval

def test_retrieval_formula_threshold_and_trace(mem):
    store, recall, ledger = mem
    target = store.propose(lesson("QA rejected the matcher: amount tolerance must use <= at the boundary",
                                  tags=["qa", "lesson"], entities=["routine:invoice_matching"]), actor="t")["memory"]
    store.propose(lesson("Report claims need cited cells", domain="reporting", derived_from=["r"]), actor="t")
    payload = recall.retrieve("amount tolerance boundary for invoice matching", domains=["invoice_matching"],
                              entities=["routine:invoice_matching"], tags=["qa"])
    top = payload["memories"][0]
    assert top["id"] == target["id"]
    s = top["signals"]
    raw = sum(WEIGHTS[k] * s[k] for k in WEIGHTS)
    assert math.isclose(top["score"], round(min(1, raw) * target["confidence"], 4), abs_tol=2e-3)
    assert s["domain"] == 1.0 and s["entity"] == 1.0 and s["layer"] == 0.6 and s["recency"] > 0.99
    assert "domain 'invoice_matching' requested" in top["retrievalReason"]
    assert all(m["score"] >= 0.35 for m in payload["memories"]) and len(payload["memories"]) <= 5
    assert "[ST]" in payload["sections"]["invoice_matching"][0]
    assert store.get(target["id"])["accessCount"] == 1
    assert ledger.entries()[-1]["action"] == "read"
    assert recall.retrieve("weather in paris")["memories"] == []


def test_superseded_memories_are_penalized(mem):
    store, recall, _ = mem
    store.propose(decision("approved"), actor="h")
    store.propose(decision("rejected"), actor="h")
    payload = recall.retrieve("invoice_po_matcher automation decision", domains=["invoice_matching"],
                              entities=["routine:invoice_matching"], threshold=0.0)
    first, second = payload["memories"][:2]
    assert "rejected" in first["content"] and second["signals"]["superseded"] == 1.0
    assert first["score"] > second["score"]


def test_feedback_deltas_and_ledger_integrity(mem, tmp_path):
    store, _, ledger = mem
    m = store.propose(lesson("x lesson"), actor="t")["memory"]
    assert store.feedback(m["id"], "helpful", actor="h")["confidence"] == 0.85
    assert store.feedback(m["id"], "not_helpful", actor="h")["confidence"] == 0.82
    wrong = store.feedback(m["id"], "wrong", actor="h")
    assert wrong["confidence"] == 0.62 and "review" in wrong["tags"]
    assert ledger.verify() is None
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace('"helpful"', '"wrong"')
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert ledger.verify() == 2


# ------------------------------------------------------------------ mining

def test_direct_capture_mines_lessons_from_run_events():
    digest = {"run_id": "r1", "routines": ["invoice_matching"], "routine": "invoice_matching",
              "automation": "invoice_po_matcher",
              "qa_rounds": [{"round": 1, "code_version": 1, "passed": False, "failed_checks": ["matches_spec"]},
                            {"round": 2, "code_version": 2, "passed": True, "failed_checks": []}],
              "qa_failure_purposes": {1: ["check 1 (within_pct on amount) passes with a non-identical value"]},
              "priority_issues": ["invoice_matching: claims 60 h/week saved, but ..."],
              "conflict_assessments": [{"task": "invoice_matching", "trusted_source": "activity",
                                        "explanation": "measured"}],
              "decision": "approve_for_human_review", "confidence": 0.85}
    mined = direct_capture(digest)
    assert any("failed QA on matches_spec" in m["content"] and "within_pct" in m["content"] for m in mined)
    assert any(m["domain"] == "invoice_matching" and "Priority Critic" in m["content"] for m in mined)
    assert all(m["refs"] for m in mined)
    assert validate_candidates({"memories": [{"content": "x", "type": "personal", "domain": "zzz",
                                              "confidence": 2}]}, {"invoice_matching"})


# ------------------------------------------------------------------ learned -> retrieved -> behaved differently

QUERY = "Which repetitive work should we automate first?"


def test_three_sessions_learn_retrieve_and_behave_differently(make_settings):
    client = TestClient(create_app(make_settings()))
    run = lambda rid, q=QUERY: client.post("/run", json={"request_id": rid, "user_query": q,
                                                         "options": {"sample_mode": True}}).json()

    # Session 1: the team proposes invoice matching; a human rejects it with a reason.
    s1 = run("s1")
    assert s1["result"]["automation"]["routine"] == "invoice_matching"
    rejected = client.post(f"/approvals/{s1['approval']['approval_id']}", json={
        "decision": "reject", "approver": "finance manager", "note": "ERP writes need my sign-off first"}).json()
    assert rejected["status"] == "rejected" and rejected["memory"]["status"] == "created"

    # Session 2: the decision is retrieved, and the team proposes something else.
    s2 = run("s2")
    assert s2["result"]["automation"]["routine"] != "invoice_matching"
    assert "invoice_matching" in s2["memory"]["human_decisions_applied"]
    trace = [json.loads(l) for l in open(s2["log_path"], encoding="utf-8")]
    applied = [e for e in trace if e["action"] == "memory_applied"]
    assert applied and "ERP writes need my sign-off first" in applied[0]["output_summary"]
    assert any(e["action"] == "hmem_retrieve" for e in trace)
    approved = client.post(f"/approvals/{s2['approval']['approval_id']}", json={
        "decision": "approve", "approver": "ops lead"}).json()
    assert approved["memory"]["status"] == "created"

    # Session 3: both decisions are respected; the third routine comes up.
    s3 = run("s3")
    chosen = {s1["result"]["automation"]["routine"], s2["result"]["automation"]["routine"],
              s3["result"]["automation"]["routine"]}
    assert len(chosen) == 3

    # A Finance-only question has no alternative: identical rejected code is escalated, not re-submitted.
    s4 = run("s4", "Where is the finance team losing time, and what should we automate first?")
    assert s4["result"]["automation"]["routine"] == "invoice_matching"
    assert s4["result"]["decision"]["decision"] == "escalate_to_human"
    assert "ERP writes need my sign-off first" in s4["result"]["decision"]["rationale"]
    assert s4["approval"] is None

    # Every run mined its events into memory through the approval boundary.
    assert all(m["status"] in ("created", "reinforced") for m in s3["memory"]["mined"])


class Scripted:
    name = "scripted/test-double"

    def __init__(self, spec):
        self.spec, self.prompts = spec, []

    def chat(self, messages, *, temperature=0.2, **params):
        system, user = messages[0]["content"], messages[1]["content"]
        self.prompts.append((system[:30], user))
        if "PM agent" in system:
            body = {"ranking": [{"routine": "invoice_matching", "estimated_hours_saved_per_week": 20,
                                 "risk": "high", "reason": "volume"}], "chosen": "invoice_matching", "spec": self.spec}
        elif "Engineer agent" in system:
            return ChatResult(content=f"```python\n{compile_spec(self.spec)}```", model="m")
        elif "You design" in system:
            body = {"cases": []}
        elif "reviewing" in system:
            body = {"advisories": []}
        elif "Manager agent" in system:
            body = {"decision": "approve_for_human_review", "rationale": "ok"}
        elif "Memory Keeper" in system:
            body = {"memories": [{"content": "Invoice matcher passed QA first time from the template spec.",
                                  "type": "factual", "domain": "invoice_matching", "tags": ["qa"],
                                  "confidence": 0.7}]}
        else:
            raise AssertionError(system[:40])
        return ChatResult(content=json.dumps(body), model="m")


def test_engineer_receives_lessons_and_keeper_mines_with_the_model(tmp_path, make_settings):
    settings = make_settings()
    store, pipeline, builder = open_world(settings)
    memory, recall = open_memory(settings, sample_mode=True)
    memory.propose(lesson("invoice_po_matcher v1 failed QA on matches_spec; failing cases: amount tolerance "
                          "boundary; v2 passed.", tags=["qa", "lesson"], entities=["routine:invoice_matching"]),
                   actor="seed")
    generator = Scripted(template_for("invoice_matching"))
    tracer = Tracer("lessons", settings.log_dir, stream=None)
    result = run_pipeline("Which repetitive work should we automate first?", {}, settings=settings, tracer=tracer,
                          generator=generator, sample_mode=True, store=store, pipeline=pipeline, builder=builder,
                          approvals=ApprovalLedger(settings.world_db_path.parent), memory=memory, recall=recall,
                          stages=["build"])
    engineer_prompt = next(u for s, u in generator.prompts if s.startswith("You are the Engineer"))
    assert "Lessons from earlier runs (H-MEM)" in engineer_prompt and "amount tolerance" in engineer_prompt
    assert result["memory"]["mined"][0]["status"] == "created"
    assert "MemoryKeeperAgent" in result["agents_used"]
    assert any("passed QA first time" in m["content"] for m in memory.all())
