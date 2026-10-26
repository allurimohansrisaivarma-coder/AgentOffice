import json

import httpx
import pytest

from app.adapters import meterless
from app.adapters.http import AdapterError
from app.adapters.meterless import meterless_client, openai_compatible_client
from app.llm import build_generator


@pytest.mark.parametrize("pasted", ["127.0.0.1:8088", "http://127.0.0.1:8088", "http://127.0.0.1:8088/",
                                    "http://127.0.0.1:8088/v1", "http://127.0.0.1:8088/v1/v1/",
                                    " http://127.0.0.1:8088/v1/chat/completions "])
def test_pasted_urls_normalize_to_host_v1(pasted):
    assert meterless.normalize_base_url(pasted) == "http://127.0.0.1:8088/v1"


def test_connect_link():
    url, key = meterless.parse_connect_link("http://192.168.1.5:8090/connect#k=abc123")
    assert (url, key) == ("http://192.168.1.5:8090/v1", "abc123")
    with pytest.raises(ValueError):
        meterless.parse_connect_link("http://192.168.1.5:8090/connect")


def test_config_reads_connect_link(isolated_env, monkeypatch):
    monkeypatch.setenv("METERLESS_CONNECT_LINK", "http://localhost:8787/connect#k=linkkey-123456")
    s = isolated_env.load_settings(env_file=None)
    assert s.meterless_base_url == "http://localhost:8787/v1"
    assert s.meterless_runtime_token == "linkkey-123456"


def test_placeholder_token_rejected(make_settings):
    with pytest.raises(AdapterError) as e:
        meterless_client(make_settings(meterless_runtime_token="rt-local"))
    assert e.value.type == "not_configured" and "rt-local" in e.value.message


def test_client_waits_for_a_model_to_load(make_settings):
    client = meterless_client(make_settings())
    assert client._client.timeout.read >= 180


def test_scope_header_only_on_chat(make_settings):
    seen = {}

    def handler(request):
        seen[request.url.path] = request.headers
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "meterless/auto"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handler)
    assert meterless.probe("http://nightshift.test/v1", "tok", app_name="agentoffice-tests",
                           transport=transport)["status"] == "online"
    client = meterless_client(make_settings(), transport=transport)
    assert client.chat([{"role": "user", "content": "x"}]).content == "ok"
    assert "meterless-scope" not in seen["/v1/models"]
    assert seen["/v1/models"]["meterless-app"] == "agentoffice-tests"
    assert seen["/v1/chat/completions"]["meterless-scope"] == "user"


@pytest.mark.parametrize("response, status", [
    (httpx.Response(401), "unauthorized"),
    (httpx.Response(403), "unauthorized"),
    (httpx.Response(200, json={"data": [{"id": "meterless/auto"}, {"id": "other"}]}), "online"),
    (httpx.Response(200, json={"data": [{"id": "gpt-x"}]}), "offline"),
    (httpx.Response(500), "offline"),
])
def test_probe_status(response, status):
    result = meterless.probe("127.0.0.1:8088", "tok", transport=httpx.MockTransport(lambda r: response))
    assert result["status"] == status


def test_probe_offline_when_refused():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    assert meterless.probe("127.0.0.1:8088", transport=httpx.MockTransport(refuse))["status"] == "offline"


def test_auto_detect_returns_first_online_or_unauthorized():
    def handler(request):
        if request.url.port == 8787 and request.url.host == "localhost":
            return httpx.Response(401)
        raise httpx.ConnectError("refused", request=request)

    found = meterless.auto_detect(transport=httpx.MockTransport(handler))
    assert found["base_url"] == "http://localhost:8787/v1" and found["status"] == "unauthorized"
    assert meterless.auto_detect(transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("x", request=r)))) is None


def test_chat_sends_nightshift_headers_and_reads_reply(make_settings):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "cmpl-1", "model": "nvidia/nemotron-test",
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"total_tokens": 7},
        })

    client = meterless_client(make_settings(), transport=httpx.MockTransport(handler))
    result = client.chat([{"role": "user", "content": "hi"}], temperature=0.1)

    assert seen["url"] == "http://nightshift.test/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer mtok-test-1234567890"
    assert seen["headers"]["meterless-app"] == "agentoffice-tests"
    assert seen["headers"]["meterless-scope"] == "user"
    assert seen["body"]["model"] == "meterless/test-route"
    assert seen["body"]["temperature"] == 0.1
    assert result.content == "hello"
    assert result.model == "nvidia/nemotron-test"   # what actually answered, for the trace
    assert result.usage == {"total_tokens": 7}


def test_malformed_reply_is_structured_error(make_settings):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": []}))
    with pytest.raises(AdapterError) as e:
        meterless_client(make_settings(), transport=transport).chat([{"role": "user", "content": "x"}])
    assert e.value.to_dict()["type"] == "meterless_invalid_response"


def test_openai_compatible_has_no_meterless_headers(make_settings):
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    s = make_settings(llm_provider="openai_compatible", model_base_url="http://other.test/v1",
                      model_api_key="okey-1234567890", model_name="some-model")
    openai_compatible_client(s, transport=httpx.MockTransport(handler)).chat(
        [{"role": "user", "content": "x"}])
    assert "meterless-app" not in seen["headers"]


def test_build_generator(make_settings):
    assert build_generator(make_settings(sample_mode=True)) is None
    assert build_generator(make_settings()).name == "meterless/test-route"
