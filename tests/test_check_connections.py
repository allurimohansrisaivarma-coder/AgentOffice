import json

import httpx

from app.tracing import verify_chain
from scripts.check_connections import main


def zetaris_ok(request: httpx.Request):
    sql = json.loads(request.content)["sql"]
    if sql == "SELECT 1 AS ok":
        return httpx.Response(200, json={"data": [{"ok": 1}]})
    return httpx.Response(200, json={"data": [{"name": "pokeapi"}, {"name": "ny_taxi"}]})


def meterless_ok(request: httpx.Request):
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [
            {"id": "meterless/test-route"}, {"id": "meterless/nemotron-fast"}]})
    return httpx.Response(200, json={
        "model": "nvidia/nemotron-test", "usage": {"total_tokens": 12},
        "choices": [{"message": {"content": "The connection works."}}]})


def refused(request: httpx.Request):
    raise httpx.ConnectError("connection refused", request=request)


def _run(settings, capsys, *argv, zetaris=zetaris_ok, meterless=meterless_ok):
    transports = {"zetaris": httpx.MockTransport(zetaris),
                  "meterless": httpx.MockTransport(meterless)}
    code = main(["--json", *argv], settings=settings, transports=transports)
    report = json.loads(capsys.readouterr().out)
    return code, {f"{r['service']}.{r['name']}": r for r in report["results"]}, report


def test_all_checks_pass(make_settings, capsys):
    code, results, report = _run(make_settings(), capsys)
    assert code == 0
    assert [r["status"] for r in results.values()] == ["pass"] * 4
    assert "2 database(s)" in results["zetaris.show_databases"]["detail"]
    assert "'meterless/test-route' listed" in results["meterless.models"]["detail"]
    assert "meterless/nemotron-fast" in results["meterless.models"]["detail"]
    assert results["meterless.chat"]["model"] == "nvidia/nemotron-test"
    assert verify_chain(report["trace_path"]) is None


def test_services_reported_separately(make_settings, capsys):
    code, results, _ = _run(make_settings(), capsys, meterless=refused)
    assert code == 1
    assert results["zetaris.select_1"]["status"] == "pass"
    assert results["meterless.models"]["status"] == "fail"
    assert "meterless_unreachable" in results["meterless.models"]["detail"]
    assert results["meterless.chat"]["status"] == "skip"


def test_zetaris_failure_skips_second_query(make_settings, capsys):
    code, results, _ = _run(make_settings(), capsys, "--only", "zetaris",
                            zetaris=lambda r: httpx.Response(401))
    assert code == 1
    assert "zetaris_auth" in results["zetaris.select_1"]["detail"]
    assert results["zetaris.show_databases"]["status"] == "skip"
    assert not any(k.startswith("meterless") for k in results)


def test_not_configured_sends_nothing(make_settings, capsys):
    def no_request(request):
        raise AssertionError("no request should be sent")

    s = make_settings(zetaris_api_key="", meterless_runtime_token="")
    code, results, _ = _run(s, capsys, zetaris=no_request, meterless=no_request)
    assert code == 1
    assert results["zetaris.config"]["detail"].startswith("not configured")
    assert "METERLESS_RUNTIME_TOKEN" in results["meterless.config"]["detail"]


def test_secrets_never_printed_or_logged(make_settings, capsys):
    def echo_auth(request):
        # A server that echoes the key back in an error must not leak it.
        return httpx.Response(401, text=f"bad key {request.headers['authorization']}")

    s = make_settings()
    code = main([], settings=s, transports={"zetaris": httpx.MockTransport(echo_auth),
                                            "meterless": httpx.MockTransport(echo_auth)})
    out = capsys.readouterr().out
    assert code == 1
    trace = next(s.log_dir.glob("trace-*.jsonl")).read_text(encoding="utf-8")
    for secret in s.secret_values():
        assert secret not in out
        assert secret not in trace
