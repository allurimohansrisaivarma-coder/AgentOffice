import json
from pathlib import Path

from fastapi.testclient import TestClient

from run import create_app
from app.tracing import verify_chain

BODY = {"request_id": "demo-001", "user_query": "Where is the finance team losing time?",
        "context": {"track": "reasoning"}, "options": {}}


def test_health(make_settings):
    r = TestClient(create_app(make_settings())).get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["missing_config"] == []


def test_run_without_config_is_structured_error(make_settings):
    app = create_app(make_settings(zetaris_api_key="", meterless_runtime_token=""))
    r = TestClient(app).post("/run", json=BODY)
    body = r.json()
    assert r.status_code == 503
    assert body["status"] == "error"
    assert body["error"]["type"] == "not_configured"
    assert body["error"]["recoverable"] is True
    assert "ZETARIS_API_KEY" in body["error"]["message"]
    assert body["trace_id"] and Path(body["log_path"]).exists()


def test_sample_run_succeeds_with_a_clean_trace(make_settings):
    s = make_settings()
    r = TestClient(create_app(s)).post("/run", json={**BODY, "options": {"sample_mode": True}})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "success"
    for key in ("request_id", "trace_id", "agents_used", "result", "collaboration", "log_path",
                "execution_time_seconds"):
        assert key in body
    assert {"summary", "findings", "confidence"} <= set(body["result"])
    assert {"feedback_loop_completed", "revision_count", "evaluator_findings"} <= set(body["collaboration"])

    log = Path(body["log_path"])
    assert verify_chain(log) is None
    text = log.read_text(encoding="utf-8")
    assert '"action": "run_started"' in text and '"action": "run_completed"' in text
    for secret in s.secret_values():
        assert secret not in text


def test_meterless_status_endpoint_never_returns_token(make_settings):
    s = make_settings(meterless_runtime_token="rt-local")
    body = TestClient(create_app(s)).get("/providers/meterless").json()
    assert body["id"] == "meterless" and body["name"] == "Meterless Runtime" and body["primary"] is True
    assert body["status"] == "not_configured" and body["token_set"] is True
    assert "rt-local" not in json.dumps({k: v for k, v in body.items() if k != "detail"})


def test_invalid_request_is_structured(make_settings):
    r = TestClient(create_app(make_settings())).post("/run", json={"request_id": "x"})
    assert r.status_code == 422
    assert r.json()["status"] == "error"
    assert r.json()["error"]["type"] == "invalid_request"
    assert "user_query" in r.json()["error"]["message"]


def test_config_exposes_meterless_and_zetaris_sponsors(make_settings):
    s = make_settings(sample_mode=False)
    body = TestClient(create_app(s)).get("/config").json()
    assert body["sponsors"]["meterless"]["ready"] is True
    assert body["sponsors"]["meterless"]["provider"] == "Meterless Nightshift"
    assert body["sponsors"]["zetaris"]["ready"] is True
    assert body["data_source"] == "Zetaris"
    assert body["forced_sample_mode"] is False


def test_replays_come_from_this_machines_own_traces(make_settings):
    s = make_settings()
    client = TestClient(create_app(s))
    trace_id = client.post("/run", json={**BODY, "options": {"sample_mode": True}}).json()["trace_id"]
    runs = client.get("/traces").json()["runs"]
    assert [r["mode"] for r in runs if r["id"] == trace_id] == ["analysis"]
    body = client.get(f"/traces/{trace_id}").json()
    assert body["chain_intact"] is True and body["count"] == len(body["events"]) > 10
    # sample mode logs its Zetaris calls as skipped, so the replay claims no sponsor call
    assert any(e["target_agent"] == "Zetaris" and e["status"] == "skipped" for e in body["events"])
    assert body["sponsors"] == {"models": ["offline-fallback"], "zetaris": False, "meterless": False}
    assert client.get("/traces/..%2F..%2Frun").status_code == 404
    assert client.get("/traces/notahexrunid0000").status_code == 404


def test_replay_reports_the_sponsor_calls_a_run_made_and_flags_an_edited_trace(make_settings):
    from app.tracing import Tracer
    s = make_settings()
    tracer = Tracer("0123456789abcdef", s.log_dir, stream=None)
    tracer.event("Orchestrator", "run_started", input_summary="Fill in the team column")
    tracer.event("DataProfilerAgent", "data_query", target_agent="Zetaris", data_source="zetaris",
                 executed_sql="SELECT 1", output_summary="1 rows")
    tracer.event("PMAgent", "plan_task", model="nvidia/nemotron-3-ultra-550b-a55b", provider="meterless/deep")
    client = TestClient(create_app(s))
    body = client.get("/traces/0123456789abcdef").json()
    assert body["mode"] == "task" and body["chain_intact"] is True
    assert body["sponsors"] == {"models": ["nvidia/nemotron-3-ultra-550b-a55b"], "zetaris": True, "meterless": True}
    path = Path(s.log_dir) / "trace-0123456789abcdef.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace("1 rows", "9 rows"), encoding="utf-8")
    assert client.get("/traces/0123456789abcdef").json()["chain_intact"] is False


def test_the_app_never_reads_output_examples():
    # output_examples/ holds saved real runs for judges; the hackathon rules forbid loading them at runtime
    root = Path(__file__).resolve().parent.parent
    for path in [root / "run.py", *(root / "app").rglob("*.py"), *(root / "app" / "ui").glob("*.js")]:
        assert "output_examples" not in path.read_text(encoding="utf-8"), path
