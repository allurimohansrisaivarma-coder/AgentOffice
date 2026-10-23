import json

import httpx
import pytest

from app.adapters.http import AdapterError
from app.adapters.meterless import meterless_client, openai_compatible_client
from app.llm import build_generator


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


def test_list_models(make_settings):
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"data": [{"id": "meterless/auto"}, {"id": "meterless/fast"}]}))
    assert meterless_client(make_settings(), transport=transport).list_models() == [
        "meterless/auto", "meterless/fast"]


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
    assert build_generator(make_settings()).name == "meterless/meterless/test-route"
