import json
import uuid

import httpx
import pytest

from app.adapters import http as http_adapter
from app.adapters.http import AdapterError
from app.adapters.zetaris import coerce, from_settings, split_statements


def _uuid(value):
    return str(uuid.UUID(value)) == value


def test_split_statements_ignores_quoted_semicolons_and_comments():
    assert split_statements("SELECT 1;") == ["SELECT 1"]
    assert split_statements("SELECT ';' AS x") == ["SELECT ';' AS x"]
    assert split_statements("SELECT 1; -- done;\n") == ["SELECT 1"]
    assert len(split_statements("SELECT 1; SELECT 2")) == 2


def test_run_sql_follows_rest_contract(make_settings):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"n": 1}]})

    client = from_settings(make_settings(), transport=httpx.MockTransport(handler))
    assert client.run_sql("SELECT 1 AS n;", limit=5) == [{"n": 1}]

    assert seen["url"] == "http://zetaris.test/api/v1.0/sql-editor/sqls/run"
    assert seen["headers"]["authorization"] == "Bearer ztok-test-1234567890"
    assert seen["headers"]["x-org-id"] == "1"
    assert _uuid(seen["headers"]["x-request-id"])
    assert seen["body"]["sql"] == "SELECT 1 AS n"
    assert seen["body"]["source"] == "SqlEditor"
    assert seen["body"]["limit"] == 5
    assert _uuid(seen["body"]["queryId"])


def test_header_and_list_rows_become_typed_records(make_settings):
    body = {"headers": ["task_category", "hours", "people", "code", "ratio"],
            "data": [["invoice_matching", "71.54", "4", "007", "1e3"], ["x", "-2", "0", "", "abc"]],
            "total": 2, "timeUsed": 12}
    client = from_settings(make_settings(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    rows = client.run_sql("SELECT 1")
    assert rows[0] == {"task_category": "invoice_matching", "hours": 71.54, "people": 4, "code": "007",
                       "ratio": 1000.0}
    assert rows[1] == {"task_category": "x", "hours": -2, "people": 0, "code": "", "ratio": "abc"}


def test_arabic_script_and_indic_digits_survive_zetaris_coerce():
    """Staff-directory keys from UAE/Saudi sheets must stay strings for arabic_* normalizers."""
    assert coerce("١٢٣") == "١٢٣"
    assert coerce("۰۱۲۳") == "۰۱۲۳"          # Persian / Extended Arabic-Indic
    assert coerce("E-١٢٣") == "E-١٢٣"
    assert coerce("١٢٫٥") == "١٢٫٥"
    assert coerce("١٬٢٣٤٫٥") == "١٬٢٣٤٫٥"
    assert coerce("أحمد") == "أحمد"
    assert coerce("007") == "007" and coerce("42") == 42


def test_arabic_directory_rows_round_trip_through_zetaris_records(make_settings):
    body = {"headers": ["staff_id", "name", "amount"],
            "data": [["E-١٢٣", "أحمد", "١٬٢٣٤٫٥٠"], ["E-456", "Sara", "12.5"]],
            "total": 2, "timeUsed": 1}
    client = from_settings(make_settings(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    rows = client.run_sql("SELECT staff_id, name, amount FROM people")
    assert rows[0] == {"staff_id": "E-١٢٣", "name": "أحمد", "amount": "١٬٢٣٤٫٥٠"}
    assert rows[1]["amount"] == 12.5


def test_list_rows_without_headers_are_rejected(make_settings):
    body = {"data": [["1"]]}
    client = from_settings(make_settings(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    with pytest.raises(AdapterError) as e:
        client.run_sql("SELECT 1")
    assert e.value.type == "invalid_response" and "headers" in e.value.message


def test_multiple_statements_rejected_before_any_request(make_settings):
    def handler(request):
        raise AssertionError("no request should be sent")

    client = from_settings(make_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterError) as e:
        client.run_sql("SELECT 1; DROP TABLE x")
    assert e.value.type == "bad_request"


def test_401_maps_to_auth_error(make_settings):
    client = from_settings(make_settings(),
                           transport=httpx.MockTransport(lambda r: httpx.Response(401, text="nope")))
    with pytest.raises(AdapterError) as e:
        client.run_sql("SELECT 1")
    assert e.value.to_dict()["type"] == "zetaris_auth"
    assert e.value.status_code == 401


def test_missing_data_is_invalid_response(make_settings):
    client = from_settings(make_settings(),
                           transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"rows": []})))
    with pytest.raises(AdapterError) as e:
        client.run_sql("SELECT 1")
    assert e.value.type == "invalid_response"


def test_reads_retry_but_writes_do_not(make_settings, monkeypatch):
    monkeypatch.setattr(http_adapter, "should_retry", lambda status: True)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["sql"])
        return httpx.Response(503)

    client = from_settings(make_settings(max_retries=2), transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterError) as e:
        client.run_sql("SELECT 1")
    assert len(calls) == 3 and e.value.attempts == 3

    calls.clear()
    with pytest.raises(AdapterError):
        client.run_sql("CREATE TABLE t (x INT)")
    assert len(calls) == 1


def test_not_configured(make_settings):
    with pytest.raises(AdapterError) as e:
        from_settings(make_settings(zetaris_api_key=""))
    assert e.value.type == "not_configured"
