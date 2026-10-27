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


def test_arabic_normalizers_unify_digits_names_and_separators():
    assert runtime.normalize("Emp-١٢٣", ["arabic_digits"]) == "Emp-123"
    assert runtime.normalize("Emp-۱۲۳", ["arabic_digits"]) == "Emp-123"          # Persian digits
    assert runtime.normalize("٠١٢", ["digits_only"]) == "012"
    assert runtime.normalize("١٢٫٥٬٠٠٠", ["arabic_digits", "arabic_separators"]) == "12.5,000"
    # أ/إ/آ → ا, ى → ي, ة → ه, tatweel and fatha stripped
    assert runtime.normalize("أحمدـ عليّة", ["arabic_text"]) == runtime.normalize("احمد عليه", ["arabic_text"])
    assert runtime.normalize("إياد", ["arabic_text"]) == "اياد"
    assert runtime.normalize("آمنة", ["arabic_text"]) == "امنه"


def test_arabic_names_folds_latin_and_arabic_given_names():
    assert runtime.normalize("محمد علي", ["arabic_names"]) == "mohammad ali"
    assert runtime.normalize("Mohammed Ali", ["arabic_names"]) == "mohammad ali"
    assert runtime.normalize("Muhammad Ali", ["arabic_names"]) == "mohammad ali"
    assert runtime.normalize("Mohamed Ali", ["arabic_names"]) == "mohammad ali"
    assert runtime.normalize("Mohd Ali", ["arabic_names"]) == "mohammad ali"
    assert runtime.normalize("Ahmed Nasser", ["arabic_names"]) == runtime.normalize("أحمد ناصر", ["arabic_names"])


def test_lookup_matches_arabic_digit_and_name_variants():
    spec = {"input": {"columns": ["staff_id", "name"]}, "references": {"staff": {"fields": ["id", "name", "team"]}},
            "steps": [
                {"op": "lookup", "key": "staff_id", "reference": "staff", "reference_key": "id",
                 "fields": {"team": "team"}, "normalize": ["strip", "arabic_digits"], "mode": "overwrite",
                 "status_column": "id_status",
                 "statuses": {"found": "ok", "not_found": "missing", "ambiguous": "dup"}},
                {"op": "lookup", "key": "name", "reference": "staff", "reference_key": "name",
                 "fields": {"team": "team"}, "normalize": ["strip", "arabic_text", "collapse_spaces"],
                 "mode": "overwrite", "status_column": "name_status",
                 "statuses": {"found": "ok", "not_found": "missing", "ambiguous": "dup"}},
            ]}
    ref = {"staff": [{"id": "E-123", "name": "احمد علي", "team": "Ops"}]}
    out = runtime.execute(spec, rows(
        {"staff_id": "E-١٢٣", "name": "x"},
        {"staff_id": "x", "name": "أحمدـ عليّ"},
    ), ref)["rows"]
    assert out[0]["team"] == "Ops" and out[0]["id_status"] == "ok"
    assert out[1]["team"] == "Ops" and out[1]["name_status"] == "ok"


def test_lookup_matches_latin_to_arabic_directory_names():
    spec = {"input": {"columns": ["name"]}, "references": {"staff": {"fields": ["name", "team"]}},
            "steps": [{"op": "lookup", "key": "name", "reference": "staff", "reference_key": "name",
                       "fields": {"team": "team"}, "normalize": ["strip", "arabic_names"], "mode": "overwrite",
                       "status_column": "status",
                       "statuses": {"found": "ok", "not_found": "missing", "ambiguous": "dup"}}]}
    ref = {"staff": [{"name": "محمد علي", "team": "Finance"}]}
    out = runtime.execute(spec, rows({"name": "Mohammed Ali"}, {"name": "Mohd Ali"}), ref)["rows"]
    assert out[0]["team"] == "Finance" and out[0]["status"] == "ok"
    assert out[1]["team"] == "Finance" and out[1]["status"] == "ok"


def test_format_date_handles_arabic_months_and_hijri():
    spec = {"input": {"columns": ["d"]}, "steps": [{"op": "format_date", "column": "d",
                                                     "input_formats": ["%d/%m/%Y", "%Y-%m-%d", "%d %b %Y"],
                                                     "output_format": "%Y-%m-%d"}]}
    out = runtime.execute(spec, rows(
        {"d": "١٥ مارس ٢٠٢٦"},
        {"d": "15 رمضان 1447"},
        {"d": "07/03/2026"},
    ), {})["rows"]
    assert out[0]["d"] == "2026-03-15"
    assert isinstance(out[1]["d"], str) and out[1]["d"].startswith("20")  # civil Hijri → Gregorian
    assert out[2]["d"] == "2026-03-07"


def test_parse_amount_strips_gulf_currency():
    spec = {"input": {"columns": ["Amount"]}, "steps": [{"op": "parse_amount", "column": "Amount"}]}
    out = runtime.execute(spec, rows(
        {"Amount": "د.إ ١٬٢٥٠٫٥٠"},
        {"Amount": "ر.س 100.00"},
        {"Amount": "AED 1,234.50"},
    ), {})["rows"]
    assert out[0]["Amount"] == 1250.5
    assert out[1]["Amount"] == 100.0
    assert out[2]["Amount"] == 1234.5


def test_uae_staff_lookup_demo_beats_naive_exact_match():
    """Measured headline: naive name join misses; AgentOffice arabic_names hits all rows."""
    path = files.DEMO_DIR / "uae_staff_lookup.xlsx"
    parsed = files.parse(path.name, path.read_bytes())
    claims = files.sheet(parsed, "Claims")["rows"]
    directory = files.sheet(parsed, "Directory")["rows"]
    naive = sum(1 for r in claims if any(d["name"] == r["Name"] for d in directory))
    assert naive == 0
    out = runtime.execute(DEMO_SPECS["uae_staff_lookup"], claims, {"directory": directory})["rows"]
    assert all(r.get("Name Match") == "matched" for r in out)
    assert all(r.get("ID Match") == "matched" for r in out)
    assert all(isinstance(r.get("Amount"), (int, float)) for r in out)


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


def test_nightshift_spec_format_lists_arabic_normalizers():
    """The PM prompt Nightshift sees must name arabic_* so Nemotron can put them on lookup keys."""
    from app.tasks.spec import spec_format
    fmt = spec_format()
    assert "arabic_digits" in fmt and "arabic_text" in fmt and "arabic_separators" in fmt
    assert "arabic_names" in fmt and "parse_amount" in fmt
    assert "UAE/Saudi" in fmt or "Zetaris staff directory" in fmt or "directory" in fmt


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


def test_critic_flags_arabic_script_variants_on_lookup_keys():
    """Keys that only match after arabic_* normalizers get a script-variant issue, not a vague miss."""
    parsed = {
        "sheets": [{"name": "Claims", "columns": ["Employee ID", "Name"], "rows": [
            {"Employee ID": "E-١٢٣", "Name": "أحمد"},
            {"Employee ID": "E-٤٥٦", "Name": "سارة"},
        ]}],
        "format": "csv", "filename": "uae.csv",
    }
    spec = {
        "name": "staff_fill", "summary": "fill from directory",
        "input": {"sheet": "Claims", "columns": ["Employee ID", "Name"]},
        "references": {"staff": {"source": "sheet", "sheet": "Staff", "fields": ["id", "name", "team"]}},
        "steps": [{"op": "lookup", "key": "Employee ID", "reference": "staff", "reference_key": "id",
                   "fields": {"Team": "team"}, "normalize": ["strip"], "mode": "fill_blanks"}],
    }
    ref = {"staff": [{"id": "E-123", "name": "احمد", "team": "Ops"},
                     {"id": "E-456", "name": "ساره", "team": "HR"}]}
    issues, _ = critique(spec, parsed, ref)
    assert any("Arabic script variants" in i and "arabic_digits" in i for i in issues)

    # Same sheet keyed on names: arabic_text is the fix.
    name_spec = copy.deepcopy(spec)
    name_spec["steps"][0]["key"] = "Name"
    name_spec["steps"][0]["reference_key"] = "name"
    issues, _ = critique(name_spec, parsed, ref)
    assert any("Arabic script variants" in i and "arabic_text" in i for i in issues)


def test_qa_mutations_keep_arabic_lookup_results_unchanged():
    spec = {"name": "id_lookup", "summary": "match staff ids",
            "input": {"sheet": "S", "columns": ["id"]},
            "references": {"staff": {"fields": ["id", "team"]}},
            "steps": [{"op": "lookup", "key": "id", "reference": "staff", "reference_key": "id",
                       "fields": {"team": "team"}, "normalize": ["strip", "arabic_digits", "arabic_text"],
                       "mode": "overwrite", "status_column": "status",
                       "statuses": {"found": "ok", "not_found": "missing", "ambiguous": "dup"}}]}
    data = rows({"id": "E-123"}, {"id": "احمد"})
    ref = {"staff": [{"id": "E-123", "team": "Ops"}, {"id": "احمد", "team": "HR"}]}
    cases = qa.build_cases(spec, data, ref, full_file=False)
    arabic_edges = [c for c in cases if c["kind"] == "edge"
                    and ("Arabic-Indic" in c["purpose"] or "Arabic spelling" in c["purpose"])]
    assert arabic_edges, "expected edge cases that swap Arabic digits / name spellings"
    baseline = runtime.execute(spec, data, ref)
    for case in arabic_edges:
        got = runtime.execute(spec, case["rows"], case["reference"])
        # Mutated key still resolves to the same directory hit the oracle expects.
        assert qa.compare(qa.expected(spec, case["rows"], case["reference"]),
                          {"ok": True, "output": got}) is None
        assert got["rows"][0]["status"] == "ok"
    assert baseline["rows"][0]["team"] == "Ops"


def test_desktop_app_reads_the_page_state_from_the_window_title():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import island
    assert island.parse_title("AgentOffice · panel · 36") == ("panel", 36)
    assert island.parse_title("AgentOffice · chat · 36") == ("chat", 36)
    assert island.parse_title("AgentOffice · quit · 36") == ("quit", 36)
    assert island.parse_title("AgentOffice · whatever") == ("island", 0)
    # Floating island is supported on Windows and macOS (browser UI elsewhere).
    assert hasattr(island, "main") and callable(island.browser_path)


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


def test_nemotron_usage_counts_only_real_model_calls(tmp_path, capsys):
    import json as _json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import nemotron_usage
    events = [{"agent_name": "PMAgent", "model": "nvidia/nemotron-3-ultra-550b-a55b", "tokens": 100},
              {"agent_name": "QAAgent", "model": "nvidia/nemotron-3-ultra-550b-a55b", "tokens": 50},
              {"agent_name": "QAAgent", "model": "offline-fallback", "tokens": None},
              {"agent_name": "Orchestrator", "model": None}]
    trace = tmp_path / "trace-x.jsonl"
    trace.write_text("\n".join(_json.dumps(e) for e in events), encoding="utf-8")
    nemotron_usage.main([str(trace)])
    out = capsys.readouterr().out
    assert "2 model calls in 1 traces, 150 tokens; answered by Nemotron: 2/2" in out


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
    # sample mode plans any file with the rule-based planner...
    sort = _wait(client, client.post("/chat", json={"message": "sort by score, highest first",
                                                    "upload_id": ok["upload_id"]}).json()["job_id"])
    task = sort["result"]["task"]
    assert task["status"] == "awaiting_approval" and task["qa"]["passed"]
    assert task["spec"]["steps"] == [{"op": "sort", "by": [{"column": "Score", "descending": True}]}]
    assert [r["Name"] for r in task["qa"]["preview"]["rows"]] == ["B", "A"]
    # ...and says what it understands when nothing matches: a clear, structured error, not a fake plan
    unknown = _wait(client, client.post("/chat", json={"message": "write a poem about this list",
                                                       "upload_id": ok["upload_id"]}).json()["job_id"])
    assert unknown["status"] == "failed" and unknown["error"]["type"] == "agent_no_offline_plan"
    assert "remove duplicates" in unknown["error"]["message"]


def test_arabic_upload_survives_zetaris_cells_and_directory_lookup(make_settings):
    """Upload → /data/uploads/cells (what Zetaris pulls) keeps Arabic; lookup with arabic_* matches ASCII directory."""
    from app.adapters.zetaris import coerce

    client = TestClient(create_app(make_settings(sample_mode=True)))
    csv = "Staff ID,Name,Amount\nE-١٢٣,أحمد,١٬٢٠٠٫٥٠\nE-٤٥٦,سارة,99\n".encode("utf-8")
    meta = client.post("/uploads?filename=uae_claims.csv", content=csv).json()
    cells = client.get("/data/uploads/cells").json()["rows"]
    ours = [c for c in cells if c["upload_id"] == meta["upload_id"]]
    assert any(c["cell_value"] == "E-١٢٣" for c in ours)
    assert any(c["cell_value"] == "أحمد" for c in ours)
    assert any(c["cell_value"] == "١٬٢٠٠٫٥٠" for c in ours)

    # Zetaris REST returns every cell as a string; coerce must not mangle Arabic keys.
    staff = [{"id": coerce("E-123"), "name": coerce("احمد"), "team": "Ops"},
             {"id": coerce("E-456"), "name": coerce("ساره"), "team": "HR"}]
    spec = {"name": "uae_staff_lookup", "summary": "Match Arabic-Indic staff IDs to the Zetaris directory.",
            "input": {"columns": ["Staff ID", "Name", "Amount"]},
            "references": {"staff": {"fields": ["id", "name", "team"]}},
            "steps": [
                {"op": "lookup", "key": "Staff ID", "reference": "staff", "reference_key": "id",
                 "fields": {"Team": "team"}, "normalize": ["strip", "arabic_digits"], "mode": "overwrite",
                 "status_column": "match",
                 "statuses": {"found": "ok", "not_found": "missing", "ambiguous": "dup"}},
            ]}
    sheet_rows = client.get(f"/uploads/{meta['upload_id']}").json()["rows"][meta["sheets"][0]["name"]]
    out = runtime.execute(spec, sheet_rows, {"staff": staff})["rows"]
    assert out[0]["Team"] == "Ops" and out[0]["match"] == "ok"
    assert out[1]["Team"] == "HR" and out[1]["match"] == "ok"
    # Engineer prompt Nightshift sees must spell out arabic_* semantics for this spec.
    assert "arabic_digits" in describe(spec)


def test_uae_demo_matches_staff_ids_in_every_digit_system(make_settings):
    """The bundled UAE demo: IDs typed as P002, P٠٠٢, P۰۰۲ or ' p002' all find the same directory row."""
    import csv
    from app.tasks.spec import DEMO_SPECS, demo_spec_for

    client = TestClient(create_app(make_settings(sample_mode=True)))
    meta = client.get("/uploads/demo_uae_site_claims").json()
    rows = meta["rows"]["Claims"]
    spec = demo_spec_for({"Claims": list(rows[0])})
    assert spec == DEMO_SPECS["uae_site_claims"]
    with open("data/sample/workforce/people.csv", encoding="utf-8") as f:
        staff = [{"person_id": p["person_id"], "team": p["team"], "role": p["role"]} for p in csv.DictReader(f)]
    out = runtime.execute(spec, rows, {"staff": staff})["rows"]
    missed = [r["Staff ID"] for r in out if r["Directory Match"] != "matched"]
    assert missed == ["P٠٩٩"]                                    # the one ID that isn't on staff
    assert any(not r["Staff ID"].isascii() for r in out if r["Directory Match"] == "matched")


def test_a_repeated_request_reuses_the_approved_automation_without_asking_the_model(tmp_path, make_settings):
    """Seen live: 'Same as last week, on this week's file: <request>' was re-planned into a new spec, so the
    human was asked again. A repeat of an approved request now reuses it (QA still re-tests it)."""
    from app.agents.base import RunContext
    from app.tasks.agents import TaskPlanner
    from app.tasks.spec import DEMO_SPECS
    from app.tracing import Tracer

    class NoModel:
        name = "must-not-be-called"

        def chat(self, messages, **params):
            raise AssertionError("the model should not re-plan an approved request")

    spec = DEMO_SPECS["vendor_invoices"]
    request = "Remove duplicate invoices, fill blank statuses with Pending and sort by due date."
    approved = {spec["name"]: {"spec": spec, "request": request, "memory": "approved by Priya"}}
    sheets = {"vendor_invoices": ["Invoice No", "Vendor", "Invoice Date", "Due Date", "Amount", "Status"]}
    ctx = RunContext(run_id="r3", settings=make_settings(), tracer=Tracer("r3", tmp_path, stream=None),
                     generator=NoModel(), data=None, store=None, pipeline=None, builder=None, sample_mode=False)
    pm = TaskPlanner(ctx)
    plan = pm.plan("Same as last week, on this week's file: " + request, {}, sheets, approved, [])
    assert plan["reuse"] == spec["name"] and plan["spec"] == spec
    assert TaskPlanner._request_key(request + "  ") == TaskPlanner._request_key(request.upper())
    # the same words on a file the spec doesn't fit are planned afresh
    assert pm._same_request(request, {"Sheet1": ["A", "B"]}, approved, set()) is None


def test_ui_is_served(make_settings):
    client = TestClient(create_app(make_settings(sample_mode=True)))
    page = client.get("/")
    assert page.status_code == 200 and 'id="island"' in page.text
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/config").json()["sample_mode"] is True
