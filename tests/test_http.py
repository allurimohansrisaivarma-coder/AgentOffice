import httpx
import pytest

from app.adapters.http import AdapterError, request, should_retry


@pytest.mark.parametrize("status", [None, 408, 429, 502, 503, 504])
def test_transient_failures_retry(status):
    assert should_retry(status) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 500])
def test_permanent_failures_do_not_retry(status):
    assert should_retry(status) is False


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_auth_failure_is_tried_once_and_not_recoverable():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401)

    with pytest.raises(AdapterError) as e:
        request(_client(handler), "GET", "http://svc.test/x", service="svc", max_retries=2)
    assert len(calls) == 1
    assert e.value.type == "auth" and e.value.recoverable is False


def test_connection_error_retries_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200, json={"ok": True})

    r = request(_client(handler), "GET", "http://svc.test/x", service="svc", max_retries=2)
    assert r.json() == {"ok": True} and len(calls) == 3


def test_retries_are_bounded():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503)

    with pytest.raises(AdapterError) as e:
        request(_client(handler), "GET", "http://svc.test/x", service="svc", max_retries=2)
    assert len(calls) == 3 and e.value.attempts == 3 and e.value.recoverable is True
