"""Workspace help: questions and email drafts grounded in the data, with personal values kept from the model."""

import json
import time

from fastapi.testclient import TestClient

from app.adapters.meterless import ChatResult
from app.assistant import assist, critique_grounding, mask_rows, unmask
from app.services import Services
from app.tasks.agents import FrontDesk
from app.tracing import Tracer
from run import create_app


class ScriptedModel:
    """Replies in order and keeps every prompt it was sent, so a test can check what the model saw."""
    name = "scripted/test"

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def chat(self, messages, **params):
        self.prompts.append(messages[-1]["content"])
        return ChatResult(content=json.dumps(self.replies.pop(0)), model=self.name, usage={}, finish_reason="stop")


def test_personal_values_become_placeholders_and_come_back():
    evidence = [{"id": "q1", "question": "q", "sql": "s", "rows": [
        {"name": "Ana Dune", "email": "ana.dune@example.com", "team": "Finance", "hours": 12.5},
        {"name": "Ana Dune", "email": "ana.dune@example.com", "team": "Finance", "hours": 3}]}]
    masked, tokens = mask_rows(evidence)
    assert masked[0]["rows"][0] == {"name": "[[P1]]", "email": "[[P2]]", "team": "Finance", "hours": 12.5}
    assert masked[0]["rows"][1]["name"] == "[[P1]]"          # same person, same placeholder
    assert unmask("Hi [[P1]] ([[P2]])", tokens) == "Hi Ana Dune (ana.dune@example.com)"


def test_grounding_critic_rejects_invented_people_and_addresses():
    masked, tokens = mask_rows([{"id": "q1", "question": "q", "sql": "s", "rows": [{"name": "Ana Dune"}]}])
    objections = critique_grounding("Hi [[P1]] and [[P7]], cc boss@example.com", masked, "email Ana", tokens)
    assert any("[[P7]]" in o for o in objections) and any("boss@example.com" in o for o in objections)
    assert critique_grounding("Hi [[P1]], cc boss@example.com", masked, "cc boss@example.com", tokens) == []


def test_grounding_critic_accepts_only_numbers_the_data_or_the_request_contain():
    evidence = [{"id": "q1", "question": "q", "sql": "s", "rows": [
        {"task_category": "Invoice processing", "hours_per_week": 46.5, "total_hours": 387.5,
         "due": "2026-10-15", "amount": "1,181.17"}]}]
    masked, tokens = mask_rows(evidence)
    text = ("Invoice processing took 46.5 hours a week (12% of 387.5), due 2026-10-15 for 1,181.17. "
            "Please reply by the 31st; about 999 claims are open. Thanks, [[P12]] team of 3.")
    objections = critique_grounding(text, masked, "reply by the 31st", tokens)
    # 46.5 and 387.5 are cells, 12% = 46.5 / 387.5, the date and amount are written in cells, 31 is in the
    # request, 3 is a small count; only 999 is invented (the bogus [[P12]] is flagged as a placeholder)
    assert [o for o in objections if "number" in o] == [
        "the number 999 is not in the rows or the request, nor a ratio or percentage of two result cells; "
        "use the figures given or leave a gap like [amount]"]
    assert any("[[P12]]" in o for o in objections)


def test_an_invented_figure_is_sent_back_and_the_revision_passes(tmp_path, make_settings):
    from app.data import load_table
    count = sum(1 for r in load_table("people")[2] if r["team"] == "Finance")
    settings = make_settings(sample_mode=True)
    services = Services(settings)
    store, pipeline, builder = services.world(True)
    model = ScriptedModel(
        {"sub_questions": [{"question": "How many people are on the Finance team?"}]},
        {"sql": "SELECT COUNT(*) AS people FROM workforce.people WHERE team = 'Finance'"},
        {"answer": "The Finance team has 41 people."},
        {"answer": f"The Finance team has {count} people."})
    out = assist("question", "How many people are on the Finance team?", None, settings=settings,
                 tracer=Tracer("a2", tmp_path, stream=None), generator=model, sample_mode=True, store=store,
                 pipeline=pipeline, builder=builder, uploads=services.uploads)
    assert out["answer"] == f"The Finance team has {count} people." and count != 41
    assert out["grounding"] == {"verified": True, "rounds": 1, "objections": []}
    assert "the number 41" in model.prompts[-1]          # the objection reached the revision prompt


def test_front_desk_routes_email_and_question_requests_offline(tmp_path, make_settings):
    from app.agents.base import RunContext
    ctx = RunContext(run_id="r1", settings=make_settings(), tracer=Tracer("r1", tmp_path, stream=None),
                     generator=None, data=None, store=None, pipeline=None, builder=None, sample_mode=True)
    desk = FrontDesk(ctx)
    assert desk.route("Draft an email to the finance team about Friday", has_file=False)["intent"] == "email"
    assert desk.route("Write an email to vendors with overdue invoices", has_file=True)["intent"] == "email"
    assert desk.route("Who approves expense claims?", has_file=False)["intent"] == "question"
    # mentioning email is not asking for one: the leads demo is still a spreadsheet task
    assert desk.route("check each lead against the CRM Contacts sheet by email", has_file=True)["intent"] == "task"


def test_sample_mode_says_it_needs_the_model_instead_of_guessing(make_settings):
    client = TestClient(create_app(make_settings(sample_mode=True)))
    job = client.post("/chat", json={"message": "Draft an email to the finance team"}).json()
    deadline = time.time() + 30
    while (view := client.get(f"/jobs/{job['job_id']}").json())["status"] == "running" and time.time() < deadline:
        time.sleep(0.1)
    assert view["status"] == "failed" and view["error"]["type"] == "agent_needs_model"


def test_email_draft_is_grounded_and_the_model_never_sees_personal_values(tmp_path, make_settings):
    settings = make_settings(sample_mode=True)
    services = Services(settings)
    store, pipeline, builder = services.world(True)
    model = ScriptedModel(
        {"sub_questions": [{"question": "Who is on the Finance team?"}]},
        {"sql": "SELECT name, email, role FROM workforce.people WHERE team = 'Finance' ORDER BY person_id LIMIT 2"},
        {"to": ["[[P2]]", "[[P4]]"], "subject": "Friday close", "body": "Hi [[P1]] and [[P3]],\n\nPlease send your "
         "AP figures by Friday.\n\nThanks"})
    out = assist("email", "Draft an email to the first two finance AP clerks asking for figures by Friday", None,
                 settings=settings, tracer=Tracer("a1", tmp_path, stream=None), generator=model, sample_mode=True,
                 store=store, pipeline=pipeline, builder=builder, uploads=services.uploads)
    assert out["to"] == ["ana.dune@example.com", "ben.ives@example.com"]
    assert out["body"].startswith("Hi Ana Dune and Ben Ives,")
    assert out["grounding"] == {"verified": True, "rounds": 0, "objections": []}
    assert out["evidence"][0]["rows"] == 2 and out["personal_values_masked"] == 4
    seen = "\n".join(model.prompts)
    assert "Ana Dune" not in seen and "example.com" not in seen
    trace = (tmp_path / "trace-a1.jsonl").read_text(encoding="utf-8")
    assert "critique_grounding" in trace and "Ana Dune" not in trace
