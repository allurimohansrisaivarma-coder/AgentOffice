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
