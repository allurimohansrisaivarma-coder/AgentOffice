"""Faster live runs (Nightshift lanes, 404 fallback, QA in parallel with the Engineer) and the sample-mode
rule-based planner for any spreadsheet."""

import threading

import pytest

from app.adapters.http import AdapterError
from app.adapters.meterless import ChatResult
from app.agents.base import Agent, RunContext
from app.data import catalog
from app.tasks.offline_planner import drop_criticized, rule_plan
from app.tasks.pipeline import _build_while_qa_designs
from app.tracing import Tracer

LIGHTNING = "nvidia/nemotron-3.5-lightning-30b-a3b"


def _ctx(settings, generator=None, tmp_path=None):
    tracer = Tracer("r1", tmp_path or settings.log_dir, stream=None)
    return RunContext(run_id="r1", settings=settings, tracer=tracer, generator=generator, data=None, store=None,
                      pipeline=None, builder=None, sample_mode=False)


# ------------------------------------------------------------------ lanes

def test_lanes_send_light_work_to_the_fast_model_without_thinking(make_settings):
    ctx = _ctx(make_settings())
    assert ctx.lane("fast") == {"model": LIGHTNING, "chat_template_kwargs": {"enable_thinking": False}}
    assert ctx.lane("draft") == ctx.lane("fast")
    assert ctx.lane("deep") == {}                     # the configured route (Nemotron 3 Ultra)
    assert _ctx(make_settings(meterless_fast_thinking=True)).lane("fast") == {"model": LIGHTNING}
    assert _ctx(make_settings(meterless_fast_model="route")).lane("fast") == {}
    assert _ctx(make_settings(llm_provider="openai_compatible")).lane("fast") == {}


class _Gen:
    name = "meterless"

    def __init__(self, fail_first=False):
        self.calls, self.fail_first = [], fail_first

    def chat(self, messages, *, temperature=0.2, **params):
        self.calls.append(params)
        if self.fail_first and len(self.calls) == 1:
            raise AdapterError("meterless", "http_error", "404: Function 'abc' Not found for account",
                               recoverable=False, status_code=404)
        return ChatResult(content="ok", model=params.get("model") or "nvidia/nemotron-3-ultra-550b-a55b")


class _Light(Agent):
    name = "LightAgent"
    tier = "fast"


def test_an_unavailable_model_is_retried_once_on_the_fallback(make_settings, tmp_path):
    gen = _Gen(fail_first=True)
    ctx = _ctx(make_settings(), gen, tmp_path)
    assert _Light(ctx).chat("s", "u", action="act") == "ok"
    assert gen.calls[0]["model"] == LIGHTNING and gen.calls[1]["model"] == "nvidia/nemotron-3-ultra-550b-a55b"
    events = [line for line in (tmp_path / "trace-r1.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any('"model_fallback"' in e for e in events) and ctx.model_calls == 2


def test_a_failing_fast_lane_escalates_once_to_the_deep_lane(make_settings, tmp_path):
    class Empty(_Gen):
        def chat(self, messages, *, temperature=0.2, **params):
            self.calls.append(params)
            if len(self.calls) == 1:
                raise AdapterError("meterless", "http_error", "stream error: lightning_empty_response",
                                   recoverable=True)
            return ChatResult(content="ok", model="nvidia/nemotron-3-ultra-550b-a55b")
    gen = Empty()
    assert _Light(_ctx(make_settings(), gen, tmp_path)).chat("s", "u", action="act") == "ok"
    assert gen.calls[0]["model"] == LIGHTNING and "model" not in gen.calls[1]


def test_tier_can_be_raised_per_call(make_settings, tmp_path):
    gen = _Gen()
    ctx = _ctx(make_settings(), gen, tmp_path)
    _Light(ctx).chat("s", "u", action="act", tier="deep")
    assert "model" not in gen.calls[0] and "chat_template_kwargs" not in gen.calls[0]


def test_qa_designs_its_tests_while_the_engineer_writes_code():
    designing = threading.Event()

    class Engineer:
        offline = False

        def build(self, spec, lessons):
            # finishes only if QA is working at the same time
            assert designing.wait(timeout=5), "QA did not run in parallel with the Engineer"
            return {"version": 1}

    class QA:
        def design_tests(self, spec, rows):
            designing.set()
            return []

    assert _build_while_qa_designs(Engineer(), QA(), {}, [], []) == {"version": 1}


def test_a_failure_in_parallel_test_design_is_not_swallowed():
    class Engineer:
        offline = False

        def build(self, spec, lessons):
            return {"version": 1}

    class QA:
        def design_tests(self, spec, rows):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _build_while_qa_designs(Engineer(), QA(), {}, [], [])


# ------------------------------------------------------------------ rule-based planner (sample mode)

ORDERS = ["Order ID", "Customer", "Email", "Order Date", "Region", "Amount", "Status"]
PROFILE = {"sheets": [{"name": "orders", "rows": 5, "columns": [
    {"name": "Order ID", "type": "text", "examples": ["A-1"]},
    {"name": "Customer", "type": "text", "examples": ["xxx xxxx"], "personal": True},
    {"name": "Email", "type": "text", "examples": ["a***@x.com"], "personal": True},
    {"name": "Order Date", "type": "text", "examples": ["03/04/2026", "2026-04-05"]},
    {"name": "Region", "type": "text", "values": ["'North' x2", "'north' x1", "'South' x2"],
     "note": "3 spellings of 2 values (case/spacing differ)"},
    {"name": "Amount", "type": "number", "examples": [1200, 80]},
    {"name": "Status", "type": "text", "values": ["'Paid' x2"]}]}]}


def _plan(task):
    return rule_plan(task, PROFILE, {"orders": ORDERS}, catalog())


def test_rule_planner_turns_a_plain_request_into_a_valid_spec():
    plan = _plan("remove duplicate order IDs, standardise order dates, fill blank Status with Pending, add 10% GST "
                 "to Amount, flag amounts over 1,000 and sort by Order Date descending, with totals by Region")
    spec = plan["spec"]
    assert [s["op"] for s in spec["steps"]] == ["dedupe", "format_date", "fill_blank", "compute", "flag", "sort",
                                                "summarize"]
    steps = {s["op"]: s for s in spec["steps"]}
    assert steps["dedupe"]["columns"] == ["Order ID"]
    assert steps["compute"] == {"op": "compute", "column": "Amount incl GST", "expression": "{Amount} * 1.1",
                                "round": 2}
    assert steps["flag"]["when"] == [{"column": "Amount", "op": "gt", "value": 1000}]
    assert steps["sort"]["by"] == [{"column": "Order Date", "descending": True}]
    assert steps["summarize"]["group_by"] == ["Region"]
    assert set(spec["input"]["columns"]) == {"Order ID", "Order Date", "Status", "Amount", "Region"}
    assert plan["left_out"] == [] and "rule-based planner" in plan["assumptions"][0]


def test_rule_planner_reads_conditions_lookups_and_leaves_out_what_it_cannot_do():
    keep = _plan("keep only rows where Region is North")["spec"]["steps"]
    assert keep == [{"op": "filter", "keep_when": [{"column": "Region", "op": "eq", "value": "North"}],
                     "match": "all"}]
    drop = _plan("remove rows with blank Status")["spec"]["steps"]
    assert drop[0]["keep_when"] == [{"column": "Status", "op": "not_blank"}]
    lookup = _plan("fill in team and role from the staff directory")["spec"]
    assert lookup["references"]["staff"]["sql"] == "SELECT email, team, role FROM workforce.people"
    assert lookup["steps"][0]["key"] == "Email" and lookup["steps"][0]["fields"] == {"Team": "team", "Role": "role"}
    # 'Total' inside a column name is not a request for totals
    sort = rule_plan("sort by Total Amount", {"sheets": []}, {"s": ["Total Amount", "Name"]}, catalog())
    assert [s["op"] for s in sort["spec"]["steps"]] == ["sort"]
    partial = _plan("sort by Amount and then translate everything to French")
    assert [s["op"] for s in partial["spec"]["steps"]] == ["sort"]
    assert any("French" in c for c in partial["left_out"])
    assert _plan("write me a poem about these customers") is None


def test_offline_revision_drops_the_steps_the_critic_rejected():
    spec = _plan("standardise order dates and sort by Amount")["spec"]
    smaller, removed = drop_criticized(spec, ["step 1 (format_date): 2 'Order Date' values match none of the input "
                                              "formats"], {"orders": ORDERS})
    assert removed == ["format_date"] and [s["op"] for s in smaller["steps"]] == ["sort"]
    assert smaller["input"]["columns"] == ["Amount"]
    assert drop_criticized(smaller, ["step 1 (sort): bad"], {"orders": ORDERS}) is None
