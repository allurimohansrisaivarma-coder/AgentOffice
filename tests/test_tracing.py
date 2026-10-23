import json

from app.tracing import Tracer, verify_chain


def _lines(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def test_writes_jsonl_with_required_fields(tmp_path):
    t = Tracer("run1", tmp_path, stream=None)
    t.event("PlannerAgent", "plan", target_agent="ExecutorAgent", confidence=0.8)
    (event,) = _lines(t.path)
    for key in ("timestamp", "run_id", "agent_name", "action", "input_summary",
                "output_summary", "target_agent", "model", "confidence",
                "retry_count", "status"):
        assert key in event
    assert event["timestamp"].endswith("Z")
    assert t.path.name == "trace-run1.jsonl"


def test_masks_known_secrets_and_key_shapes(tmp_path):
    t = Tracer("run2", tmp_path, secrets=["ztok-supersecret-value"], stream=None)
    t.event("Executor", "call", input_summary="key=ztok-supersecret-value",
            output_summary="Authorization: Bearer abcdefghijklmnop sk-1234567890abcdef",
            extra_field={"nested": "nvapi-abcdefghijkl"})
    text = t.path.read_text(encoding="utf-8")
    assert "ztok-supersecret-value" not in text
    assert "abcdefghijklmnop" not in text
    assert "sk-1234567890abcdef" not in text
    assert "nvapi-abcdefghijkl" not in text
    assert "Bearer ***redacted" in text


def test_hash_chain_detects_tampering(tmp_path):
    t = Tracer("run3", tmp_path, stream=None)
    for i in range(3):
        t.event("Agent", f"step{i}")
    assert verify_chain(t.path) is None

    lines = t.path.read_text(encoding="utf-8").splitlines()
    edited = json.loads(lines[1])
    edited["status"] = "approved"
    lines[1] = json.dumps(edited)
    t.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify_chain(t.path) == 2
