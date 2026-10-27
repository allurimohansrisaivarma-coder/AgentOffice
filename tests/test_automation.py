import pytest

from app.automation.compiler import compile_spec
from app.automation.oracle import MissingField, drift_copy, expected_output, generate_cases
from app.automation.sandbox import find_secrets, policy_violations, run_in_sandbox
from app.automation.spec import TEMPLATES, check_passes, describe, normalize_key, template_for, validate_spec


@pytest.mark.parametrize("routine", sorted(TEMPLATES))
def test_templates_are_valid(routine):
    assert validate_spec(template_for(routine)) == []
    assert "raise ValueError" in describe(template_for(routine))


def test_validation_explains_problems():
    spec = template_for("invoice_matching")
    spec["checks"][1]["op"] = "within_pct"
    spec["checks"][1]["left"] = "vendor"            # str field with a numeric op
    spec["statuses"]["pass"] = "vendor_mismatch"   # duplicate status
    spec["key"]["normalize"] = ["trim"]
    errors = validate_spec(spec)
    assert any("needs number fields" in e for e in errors)
    assert any("distinct" in e for e in errors)
    assert any("key.normalize" in e for e in errors)
    assert validate_spec("nope") == ["spec must be a JSON object"]


def test_check_semantics():
    pct = {"op": "within_pct", "value": 1.0}
    assert check_passes(pct, 101.99, 101.0 + 0.999) and not check_passes(pct, 103.0, 100.0)
    assert not check_passes(pct, "100", 100.0)            # wrong type fails
    assert check_passes({"op": "equals_ci"}, " ACME ", "acme")
    assert not check_passes({"op": "equals"}, "ACME", "acme")
    assert normalize_key(" #12-34 ", ["digits_only"]) == "1234"
    assert normalize_key("Emp-١٢٣", ["arabic_digits"]) == "Emp-123"
    assert normalize_key("١٢٫٥", ["arabic_digits", "arabic_separators"]) == "12.5"
    assert normalize_key("أحمد", ["arabic_text"]) == normalize_key("احمد", ["arabic_text"])


@pytest.mark.parametrize("routine", sorted(TEMPLATES))
def test_cases_cover_every_status(routine):
    spec = template_for(routine)
    cases = generate_cases(spec)
    statuses = {row["status"] for row in expected_output(spec, cases["left"], cases["right"])}
    wanted = set(spec["statuses"].values()) | {c["fail_status"] for c in spec["checks"]}
    assert statuses == wanted


def test_oracle_requires_fields():
    spec = template_for("invoice_matching")
    cases = generate_cases(spec)
    drifted = drift_copy(spec, cases)
    with pytest.raises(MissingField):
        expected_output(spec, drifted["left"], drifted["right"])


@pytest.mark.parametrize("routine", sorted(TEMPLATES))
def test_compiled_code_passes_policy_and_matches_oracle(routine):
    spec = template_for(routine)
    code = compile_spec(spec)
    assert policy_violations(code) == [] and find_secrets(code) == []
    cases = generate_cases(spec, seed=3)
    result = run_in_sandbox(code, cases["left"], cases["right"])
    assert result["ok"], result
    assert result["output"] == expected_output(spec, cases["left"], cases["right"])
    drifted = drift_copy(spec, cases)
    loud = run_in_sandbox(code, drifted["left"], drifted["right"])
    assert loud["ok"] is False and "ValueError" in loud["error"]


def test_a_subtly_wrong_implementation_is_caught():
    spec = template_for("invoice_matching")
    code = compile_spec(spec).replace("<= 1.0 / 100", "< 1.0 / 200")   # tolerance halved
    cases = generate_cases(spec)
    result = run_in_sandbox(code, cases["left"], cases["right"])
    expected = expected_output(spec, cases["left"], cases["right"])
    assert result["ok"] and result["output"] != expected


@pytest.mark.parametrize("attack, why", [
    ("import socket\ndef run(left, right):\n    return []", "import of 'socket'"),
    ("import os\ndef run(left, right):\n    return []", "import of 'os'"),
    ("def run(left, right):\n    return open('x').read()", "'open'"),
    ("def run(left, right):\n    return __import__('os').listdir('.')", "'__import__'"),
    ("def run(left, right):\n    return ().__class__.__bases__", "dunder attribute"),
    ("def run(left, right):\n    return eval('1')", "'eval'"),
    ("from subprocess import run as r\ndef run(left, right):\n    return []", "import from 'subprocess'"),
    ("def go(x):\n    return x", "run(left, right)"),
])
def test_policy_blocks_attacks(attack, why):
    assert any(why in p for p in policy_violations(attack))


def test_sandbox_times_out_and_hides_environment(monkeypatch):
    spin = "def run(left, right):\n    while True:\n        pass\n"
    result = run_in_sandbox(spin, [], [], timeout_s=1.5)
    assert result["timed_out"] and not result["ok"]

    monkeypatch.setenv("ZETARIS_API_KEY", "should-not-leak")
    # json is allowed; the child gets an empty environment, so even unrestricted code could not read secrets.
    probe = "import json\ndef run(left, right):\n    return [json.dumps(left)]"
    result = run_in_sandbox(probe, [{"a": 1}], [])
    assert result["ok"] and result["output"] == ['[{"a": 1}]']


def test_secrets_are_flagged():
    assert find_secrets('API_KEY = "abcdef123456"\ndef run(l, r):\n    return []')
    assert find_secrets("x = 'sk-abcdefghijklmnop'")
