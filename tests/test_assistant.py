"""Workspace help: questions and email drafts grounded in the data, with personal values kept from the model."""

import json
import time

from fastapi.testclient import TestClient

from app.adapters.meterless import ChatResult
from app.assistant import assist, critique_grounding, critique_recipients, mask_rows, unmask
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
        {"answer": f"The Finance team has {count} people."},
        {"objections": []})                               # the wording review, once the rules pass
    out = assist("question", "How many people are on the Finance team?", None, settings=settings,
                 tracer=Tracer("a2", tmp_path, stream=None), generator=model, sample_mode=True, store=store,
                 pipeline=pipeline, builder=builder, uploads=services.uploads)
    assert out["answer"] == f"The Finance team has {count} people." and count != 41
    assert out["grounding"] == {"verified": True, "rounds": 1, "objections": []}
    assert "the number 41" in model.prompts[-2]          # the objection reached the revision prompt
    assert len(model.prompts) == 5                       # the rules caught 41 before any wording review ran


def test_recipients_must_be_people_not_ids():
    message = "Draft an email to the finance team and cc boss@example.com"
    assert critique_recipients(["[[P2]]", "boss@example.com", "Finance team"], message) == []
    objections = critique_recipients(["P001", "[[P4]]"], message)
    assert len(objections) == 1 and "'P001'" in objections[0]


def test_an_email_addressed_to_ids_is_sent_back_and_comes_back_to_people(tmp_path, make_settings):
    settings = make_settings(sample_mode=True)
    services = Services(settings)
    store, pipeline, builder = services.world(True)
    model = ScriptedModel(
        {"sub_questions": [{"question": "Names and emails of the Finance AP clerks"}]},
        {"sql": "SELECT person_id, name, email FROM workforce.people WHERE team = 'Finance' AND role = 'AP clerk' "
                "ORDER BY person_id"},
        {"to": ["P001", "P002"], "subject": "AP close", "body": "Hi both,\n\nPlease send your figures.\n\nThanks"},
        {"to": ["[[P2]]", "[[P4]]"], "subject": "AP close", "body": "Hi [[P1]] and [[P3]],\n\nPlease send your "
         "figures.\n\nThanks"},
        {"objections": []})
    out = assist("email", "Draft an email to the Finance AP clerks asking for their figures", None, settings=settings,
                 tracer=Tracer("a3", tmp_path, stream=None), generator=model, sample_mode=True, store=store,
                 pipeline=pipeline, builder=builder, uploads=services.uploads)
    assert out["to"] == ["ana.dune@example.com", "ben.ives@example.com"]
    assert out["grounding"] == {"verified": True, "rounds": 1, "objections": []}
    assert "recipient 'P001'" in model.prompts[-2]


def test_a_figure_tied_to_the_wrong_week_is_caught_by_the_wording_review(tmp_path, make_settings):
    settings = make_settings(sample_mode=True)
    services = Services(settings)
    store, pipeline, builder = services.world(True)
    week = "CAST((CAST(substr(work_date, 9, 2) AS INTEGER) - 1) / 7 AS INTEGER) + 1"
    model = ScriptedModel(
        {"sub_questions": [{"question": "Finance hours per week in September"}]},
        {"sql": f"SELECT {week} AS week, SUM(t.hours) AS hours FROM workforce.timesheets t JOIN workforce.people p "
                f"ON p.person_id = t.person_id WHERE p.team = 'Finance' AND substr(work_date, 1, 7) = '2026-09' "
                f"GROUP BY {week} ORDER BY week"},
        {"answer": "Weeks 1 and 2 had the same hours."},  # no figure at all, so the rules have nothing to catch
        {"objections": [{"statement": "Weeks 1 and 2 had the same hours.", "reason": "week 1 and week 2 differ"}]},
        {"answer": "Week 2 had more hours than week 1."},
        {"objections": []})
    out = assist("question", "Did Finance work the same hours in the first two weeks of September?", None,
                 settings=settings, tracer=Tracer("a4", tmp_path, stream=None), generator=model, sample_mode=True,
                 store=store, pipeline=pipeline, builder=builder, uploads=services.uploads)
    assert out["answer"] == "Week 2 had more hours than week 1."
    assert out["grounding"] == {"verified": True, "rounds": 1, "objections": []}
    assert "week 1 and week 2 differ" in model.prompts[-2]   # the review's objection reached the revision
    assert '"week": 1' in model.prompts[3]                   # the review saw the rows it was judging


def test_a_result_cut_off_at_the_row_cap_reaches_the_writer_marked_truncated(tmp_path, make_settings):
    settings = make_settings(sample_mode=True)
    services = Services(settings)
    store, pipeline, builder = services.world(True)
    model = ScriptedModel(
        {"sub_questions": [{"question": "Every timesheet entry"}]},
        {"sql": "SELECT entry_id, hours FROM workforce.timesheets ORDER BY entry_id"},
        {"answer": "Here are the first entries; the list may be incomplete."},
        {"objections": []})
    out = assist("question", "List every timesheet entry", None, settings=settings,
                 tracer=Tracer("a5", tmp_path, stream=None), generator=model, sample_mode=True, store=store,
                 pipeline=pipeline, builder=builder, uploads=services.uploads)
    assert out["evidence"] == [{"question": "Every timesheet entry", "sql": out["evidence"][0]["sql"], "rows": 30,
                                "truncated": True}]
    assert '"truncated": true' in model.prompts[2]           # the writer is told the list is incomplete


def test_sql_guidance_warns_that_zetaris_division_is_decimal():
    # 8 / 7 is 1 on SQLite but 1.142857 on Zetaris; agents are told to CAST whole numbers
    from app.data import PORTABLE_SQL_NOTE
    assert "CAST(... AS INTEGER)" in PORTABLE_SQL_NOTE and "week of the month" in PORTABLE_SQL_NOTE


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


def test_front_desk_keeps_an_explicit_email_ask_when_the_model_routes_it_elsewhere(tmp_path, make_settings):
    """Seen live: the fast routing model sent 'Draft an email ... hours per week' to a full analysis run."""
    from app.agents.base import RunContext
    model = ScriptedModel({"intent": "analysis", "reply": "Let me pull the data."},
                          {"intent": "analysis", "reply": "Looking into it."})
    ctx = RunContext(run_id="r2", settings=make_settings(), tracer=Tracer("r2", tmp_path, stream=None),
                     generator=model, data=None, store=None, pipeline=None, builder=None, sample_mode=False)
    desk = FrontDesk(ctx)
    msg = "Draft an email to the Finance team summarising which task categories took the most hours per week."
    assert desk.route(msg, has_file=False)["intent"] == "email"
    # a real analysis question stays with the model's routing
    assert desk.route("Where is the finance team losing time?", has_file=False)["intent"] == "analysis"


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
         "AP figures by Friday.\n\nThanks"},
        {"objections": []})
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
