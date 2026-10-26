"""Task mode: step semantics, spec checks, files, QA against the oracle, and the chat/approve/run flow."""

import copy
import time

import pytest
from fastapi.testclient import TestClient

from app.automation.sandbox import policy_violations
from app.tasks import files, qa, runtime
from app.tasks.agents import _emit_cell_writes, _mask, _summarize_source, critique
from app.tasks.spec import DEMO_SPECS, describe, demo_spec_for, validate_spec
from run import create_app


def rows(*records):
    return [dict(r) for r in records]


# ------------------------------------------------------------------ runtime (the oracle)

def test_lookup_fills_blanks_and_reports_status():
    spec = {"input": {"columns": ["email"]}, "references": {"staff": {"fields": ["email", "team"]}}, "steps": [
        {"op": "lookup", "key": "email", "reference": "staff", "reference_key": "email", "fields": {"team": "team"},
         "normalize": ["strip", "lower"], "status_column": "status",
         "statuses": {"found": "ok", "not_found": "missing", "ambiguous": "dup"}}]}
    ref = {"staff": [{"email": "a@x.com", "team": "Ops"}, {"email": "b@x.com", "team": "Sales"},
                     {"email": "b@x.com", "team": "Admin"}]}
    out = runtime.execute(spec, rows({"email": " A@X.COM "}, {"email": "b@x.com"}, {"email": "c@x.com"},
                                     {"email": None}), ref)["rows"]
    assert [(r["team"], r["status"]) for r in out] == [("Ops", "ok"), (None, "dup"), (None, "missing"),
                                                       (None, "missing")]


def test_compute_treats_text_and_zero_division_as_blank():
    spec = {"input": {"columns": ["a", "b"]}, "steps": [
        {"op": "compute", "column": "c", "expression": "({a} + 1) / {b}", "round": 2}]}
    out = runtime.execute(spec, rows({"a": 1, "b": 3}, {"a": "1", "b": 3}, {"a": 1, "b": 0}), {})["rows"]
    assert [r["c"] for r in out] == [0.67, None, None]


def test_dedupe_sort_flag_and_summarize():
    spec = {"input": {"columns": ["id", "due", "amt"]}, "steps": [
        {"op": "dedupe", "columns": ["id"], "normalize": ["strip", "upper"]},
        {"op": "flag", "column": "late", "when": [{"column": "due", "op": "lt", "value": "2026-10-15"}],
         "then": "yes", "else": "no"},
        {"op": "sort", "by": [{"column": "due", "descending": True}]},
        {"op": "summarize", "sheet": "S", "group_by": ["late"], "values": [{"column": "amt", "agg": "sum", "as": "t"}]}]}
    data = rows({"id": "x1", "due": "2026-10-01", "amt": 5}, {"id": " X1 ", "due": "2026-11-01", "amt": 7},
                {"id": "y", "due": None, "amt": 2}, {"id": "z", "due": "2026-12-01", "amt": "n/a"})
    out = runtime.execute(spec, data, {})
    assert [r["id"] for r in out["rows"]] == ["z", "x1", "y"]           # blanks last even when descending
    assert [r["late"] for r in out["rows"]] == ["no", "yes", "no"]
    # groups in order of first appearance after the sort; "n/a" is not a number, so it adds nothing
    assert out["sheets"]["S"] == [{"late": "no", "t": 2}, {"late": "yes", "t": 5}]


def test_execute_raises_on_a_renamed_column():
    with pytest.raises(ValueError, match="Email"):
        runtime.execute({"input": {"columns": ["Email"]}, "steps": []}, rows({"E-mail": "a"}), {})


def test_format_date_tries_formats_in_order_and_keeps_invalid():
    spec = {"input": {"columns": ["d"]}, "steps": [{"op": "format_date", "column": "d",
                                                     "input_formats": ["%d/%m/%Y", "%Y-%m-%d"],
                                                     "output_format": "%Y-%m-%d"}]}
    out = runtime.execute(spec, rows({"d": "07/03/2026"}, {"d": "2026-03-07"}, {"d": "soon"}), {})["rows"]
    assert [r["d"] for r in out] == ["2026-03-07", "2026-03-07", "soon"]


# ------------------------------------------------------------------ spec validation

def test_validate_spec_checks_columns_against_the_file():
    sheets = {"Claims": ["Claim ID", "Employee Email", "Date", "Category", "Description", "Amount", "Team", "Role"]}
    assert validate_spec(DEMO_SPECS["expense_claims"], sheets) == []
    bad = copy.deepcopy(DEMO_SPECS["expense_claims"])
    bad["steps"][3]["expression"] = "{Amout} * 1.1"
    bad["input"]["columns"].append("Cost centre")
    errors = validate_spec(bad, sheets)
    assert any("Amout" in e for e in errors) and any("Cost centre" in e for e in errors)
    assert validate_spec({"name": "x", "summary": "s", "input": {"sheet": "Nope"}, "steps": []}, sheets)


def test_describe_only_explains_the_ops_used():
    text = describe(DEMO_SPECS["vendor_invoices"])
    assert "dedupe:" in text and "sort:" in text and "lookup:" not in text and "run(rows, reference)" in text


# ------------------------------------------------------------------ files

def test_demo_files_parse_and_match_their_specs():
    for path in sorted(files.DEMO_DIR.iterdir()):
        parsed = files.parse(path.name, path.read_bytes())
        assert demo_spec_for(files.sheet_columns(parsed)) is not None, path.name


def test_csv_numbers_keep_ids_with_leading_zeros_as_text():
    parsed = files.parse("t.csv", b"id,amount,code\n1,12.50,007\n2,,x\n")
    assert parsed["sheets"][0]["rows"] == [{"id": 1, "amount": 12.5, "code": "007"},
                                           {"id": 2, "amount": None, "code": "x"}]


def test_bad_uploads_are_rejected_in_words():
    with pytest.raises(files.FileError, match=".csv or .xlsx"):
        files.parse("notes.txt", b"hello")
    with pytest.raises(files.FileError, match="Excel"):
        files.parse("fake.xlsx", b"not a zip")


def test_personal_values_are_masked_for_the_model():
    assert _mask("Employee Email", " jo.smith@example.com ") == " j***@example.com "
    assert _mask("Full Name", "Jo Smith") == "xx xxxxx"
    assert _mask("Category", "Travel") == "Travel"


# ------------------------------------------------------------------ QA

def test_compiled_spec_passes_the_sandbox_policy_and_the_oracle():
    spec = DEMO_SPECS["vendor_invoices"]
    code = qa.compile_spec(spec)
    assert policy_violations(code) == []


def test_qa_catches_a_careless_implementation():
    spec = DEMO_SPECS["vendor_invoices"]
    path = files.DEMO_DIR / "vendor_invoices.csv"
    parsed = files.parse(path.name, path.read_bytes())
    data = parsed["sheets"][0]["rows"]
    careless = {"rows": [dict(r) for r in data], "sheets": {}}     # did nothing
    cases = qa.build_cases(spec, data, {})
    sample = cases[0]
    problem = qa.compare(qa.expected(spec, sample["rows"], {}), {"ok": True, "output": careless})
    assert problem and "rows" in problem
    assert any(c["kind"] == "edge" and "dedupe" in c["purpose"] for c in cases)


def test_critic_flags_a_lookup_that_matches_nothing():
    spec = copy.deepcopy(DEMO_SPECS["expense_claims"])
    path = files.DEMO_DIR / "expense_claims.xlsx"
    parsed = files.parse(path.name, path.read_bytes())
    issues, _ = critique(spec, parsed, {"staff": [{"email": "nobody@example.org", "team": "X", "role": "Y"}]})
    assert any("none of the" in i for i in issues)


@pytest.mark.skipif(__import__("os").name != "nt", reason="the desktop island is Windows-only")
def test_desktop_app_reads_the_page_state_from_the_window_title():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import island
    assert island.parse_title("AgentOffice · panel · 36") == ("panel", 36)
    assert island.parse_title("AgentOffice · chat · 36") == ("chat", 36)
    assert island.parse_title("AgentOffice · quit · 36") == ("quit", 36)
    assert island.parse_title("AgentOffice · whatever") == ("island", 0)


def test_a_provider_error_in_an_optional_qa_call_does_not_fail_the_run(tmp_path, make_settings):
    from app.adapters.http import AdapterError
    from app.agents.base import RunContext
    from app.tasks.agents import TaskQA
    from app.tracing import Tracer
    ctx = RunContext(run_id="r1", settings=make_settings(), tracer=Tracer("r1", tmp_path, stream=None),
                     generator=object(), data=None, store=None, pipeline=None, builder=None, sample_mode=False)
    agent = TaskQA(ctx)

    def outage(*args, **kwargs):
        raise AdapterError("meterless", "http_error", "404: Function not found", recoverable=True)
    agent.chat_json = outage
    assert agent._cases_from_model(DEMO_SPECS["vendor_invoices"], ["Invoice No"], {}) == []
    assert "404" in (tmp_path / "trace-r1.jsonl").read_text(encoding="utf-8")


def test_trace_survives_a_console_that_cannot_print_unicode(tmp_path):
    import io
    from app.tracing import Tracer
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    tracer = Tracer("t1", tmp_path, stream=console)
    tracer.event("QAAgent", "review_code", output_summary="v1 → v2 passed ✓")
    assert "→" in (tmp_path / "trace-t1.jsonl").read_text(encoding="utf-8")


# ------------------------------------------------------------------ streamed model replies

def test_nightshift_chat_is_streamed_and_reassembled(make_settings):
    import httpx
    from app.adapters.meterless import meterless_client

    seen = {}

    def handler(request):
        seen["body"] = request.read()
        events = [{"id": "c1", "model": "nvidia/nemotron", "choices": [{"delta": {"content": "thinking</think>{\"a\""}}]},
                  {"choices": [{"delta": {"content": ": 1}"}, "finish_reason": "stop"}]},
                  {"choices": [], "usage": {"total_tokens": 42}}]
        text = "".join(f"data: {__import__('json').dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=text.encode())

    client = meterless_client(make_settings(), transport=httpx.MockTransport(handler))
    result = client.chat([{"role": "user", "content": "hi"}])
    assert b'"stream": true' in seen["body"] or b'"stream":true' in seen["body"]
    assert result.content == 'thinking</think>{"a": 1}' and result.finish_reason == "stop"
    assert result.model == "nvidia/nemotron" and result.usage == {"total_tokens": 42}


# ------------------------------------------------------------------ Sheet pane cell events

class _Logger:
    def __init__(self):
        self.events = []

    def log(self, action, **fields):
        self.events.append({"action": action, **fields})


def _cell_events(spec, data, reference=None, after=None):
    reference = reference or {}
    out = runtime.execute(spec, data, reference)
    columns = runtime.output_columns(spec, list(data[0]))
    agent = _Logger()
    cells, changed = _emit_cell_writes(agent, "S", data, out["rows"] if after is None else after, columns,
                                       out["sheets"], reference, spec=spec, code_hash="f" * 64)
    return cells, changed, agent.events


def test_cell_writes_follow_rows_that_dedupe_and_sort_move():
    spec = {"input": {"columns": ["id", "due", "status"]}, "steps": [
        {"op": "dedupe", "columns": ["id"], "normalize": ["strip", "upper"]},
        {"op": "fill_blank", "column": "status", "value": "Pending"},
        {"op": "sort", "by": [{"column": "due"}]}]}
    data = rows({"id": "b", "due": "2026-10-03", "status": "Paid"}, {"id": "a", "due": "2026-10-01", "status": None},
                {"id": " B ", "due": "2026-10-02", "status": None})
    cells, changed, events = _cell_events(spec, data)
    # output: a (input row 2, status filled), b (input row 1, unchanged)
    real = [c for c in cells if c["kind"] == "changed"]
    assert changed == 1 and [(c["row"], c["col"], c["old"], c["value"], c["source_row"]) for c in real] == [
        (1, "status", None, "Pending", 2)]
    assert real[0]["step_label"] == "Plan step 2: fill blanks"
    moved = {(c["row"], c["col"]) for c in cells if c["kind"] == "moved"}
    assert moved == {(1, "id"), (1, "due"), (2, "id"), (2, "due"), (2, "status")}
    assert all("remove duplicates and sort rows" in c["note"] for c in cells if c["kind"] == "moved")
    assert {e["rows_out"] for e in events} == {2}


def test_cell_writes_report_no_count_when_lineage_cannot_be_shown():
    spec = {"input": {"columns": ["id"]}, "steps": [{"op": "sort", "by": [{"column": "id"}]}]}
    data = rows({"id": "b"}, {"id": "a"})
    cells, changed, _ = _cell_events(spec, data, after=rows({"id": "a"}, {"id": "zz"}))   # not the oracle's output
    assert changed is None and {c["kind"] for c in cells} == {"written"}


def test_cell_events_mask_personal_columns_but_the_result_keeps_values():
    spec = {"input": {"columns": ["Employee Name", "email"]}, "references": {"staff": {"fields": ["email", "team"]}},
            "steps": [{"op": "lookup", "key": "email", "reference": "staff", "reference_key": "email",
                       "fields": {"team": "team"}},
                      {"op": "summarize", "sheet": "By person", "group_by": ["Employee Name"],
                       "values": [{"column": "team", "agg": "count", "as": "n"}]}]}
    data = rows({"Employee Name": "Ada Lovelace", "email": "ada@example.com"})
    cells, changed, events = _cell_events(spec, data, {"staff": [{"email": "ada@example.com", "team": "Ops"}]})
    assert changed == 1 and any(c["value"] == "Ada Lovelace" for c in cells)
    logged = [c for e in events for c in e["cells"]]
    assert {c["value"] for c in logged if c["col"] == "Employee Name"} == {"xxx xxxxxxxx"}
    assert all(c["masked"] for c in logged if c["col"] == "Employee Name")
    assert {c["value"] for c in logged if c["col"] == "team"} == {"Ops"}
    assert "Ada" not in str(events) and "ada@example.com" not in str(events)


def test_summarize_label_names_the_aggregation_it_ran():
    step = {"op": "summarize", "sheet": "T", "group_by": ["Team"],
            "values": [{"column": "Claim ID", "agg": "count", "as": "Claims"}, {"column": "Amount", "agg": "avg", "as": "Avg"}]}
    assert _summarize_source(step) == "count(Claim ID), avg(Amount) by Team"


# ------------------------------------------------------------------ API flow (sample mode)

def _wait(client, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = client.get(f"/jobs/{job_id}").json()
        if view["status"] != "running":
            return view
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_chat_task_needs_approval_before_any_file_is_written(make_settings):
    client = TestClient(create_app(make_settings(sample_mode=True)))
    samples = {s["upload_id"]: s for s in client.get("/samples").json()["samples"]}
    job = client.post("/chat", json={"message": samples["demo_vendor_invoices"]["suggested_prompt"],
                                     "upload_id": "demo_vendor_invoices"}).json()
    view = _wait(client, job["job_id"])
    assert view["status"] == "completed", view["error"]
    task = view["result"]["task"]
    assert task["status"] == "awaiting_approval" and task["outputs"] is None and task["qa"]["passed"]
    agents = {e["agent"] for e in view["events"]}
    assert {"FrontDeskAgent", "DataProfilerAgent", "PMAgent", "SpecCritic", "EngineerAgent", "QAAgent",
            "ManagerAgent"} <= agents

    wrong = client.post(f"/tasks/{task['task_id']}/decision",
                        json={"decision": "approve", "approver": "tester", "code_hash": "0" * 64})
    assert wrong.status_code == 409
    done = client.post(f"/tasks/{task['task_id']}/decision",
                       json={"decision": "approve", "approver": "tester", "code_hash": task["code_hash"]}).json()
    assert done["status"] == "completed" and done["outputs"]["rows_out"] == 30
    # dedupe + sort move rows; the count still compares each output row with its own input row
    assert done["outputs"]["sheet_layout"] == {"sheet": task["spec"]["input"]["sheet"], "rows": 30}
    real = [c for c in done["outputs"]["cell_writes"] if c["kind"] == "changed"]
    assert done["outputs"]["cells_changed"] == len(real) > 0
    formats = {f["format"] for f in done["outputs"]["files"]}
    assert formats == {"xlsx", "csv"}
    download = client.get(f"/files/{done['outputs']['files'][0]['file_id']}")
    assert download.status_code == 200 and download.content[:2] == b"PK"
    assert client.get("/files/..%2Frun.py").status_code == 404

    again = _wait(client, client.post("/chat", json={"message": "same again please",
                                                     "upload_id": "demo_vendor_invoices"}).json()["job_id"])
    second = again["result"]["task"]
    assert second["reused"] == "invoice_register_cleanup" and second["status"] == "completed"
    assert second["memo"]["decision"] == "already_approved"


def test_upload_endpoint_and_reply_intent(make_settings):
    client = TestClient(create_app(make_settings(sample_mode=True)))
    bad = client.post("/uploads?filename=x.txt", content=b"hi")
    assert bad.status_code == 422 and bad.json()["error"]["type"] == "bad_file"
    ok = client.post("/uploads?filename=list.csv", content=b"Name,Score\nA,1\nB,2\n").json()
    assert ok["sheets"] == [{"name": "list", "columns": ["Name", "Score"], "rows": 2}]
    cells = client.get("/data/uploads/cells").json()["rows"]
    assert any(c["upload_id"] == ok["upload_id"] and c["col_name"] == "Score" for c in cells)
    reply = _wait(client, client.post("/chat", json={"message": "hello"}).json()["job_id"])
    assert reply["result"]["type"] == "reply"
    # sample mode cannot plan an arbitrary file: a clear, structured error, not a fake plan
    unknown = _wait(client, client.post("/chat", json={"message": "sort by score",
                                                       "upload_id": ok["upload_id"]}).json()["job_id"])
    assert unknown["status"] == "failed" and unknown["error"]["type"] == "agent_no_offline_plan"


def test_ui_is_served(make_settings):
    client = TestClient(create_app(make_settings(sample_mode=True)))
    page = client.get("/")
    assert page.status_code == 200 and 'id="island"' in page.text
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/config").json()["sample_mode"] is True
