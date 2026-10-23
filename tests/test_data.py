import pytest
from fastapi.testclient import TestClient

from app.adapters.http import AdapterError
from app.data import SampleSQLClient, ZetarisSource, build_data_source, catalog, load_table
from run import create_app
from scripts import generate_workforce_data as gen
from scripts.load_zetaris import statements, verification_queries


@pytest.fixture(scope="module")
def sql():
    client = SampleSQLClient()
    yield client
    client.close()


def test_generator_is_deterministic_and_committed_files_match():
    assert gen.render() == gen.render()
    assert gen.main(["--check"]) == 0


def test_generated_data_contains_patterns_not_answers(sql):
    # Ticket logs under-report invoice matching relative to timesheets...
    (row,) = sql.run_sql(
        "SELECT (SELECT SUM(logged_minutes) FROM workforce.tickets WHERE category = 'invoice_matching') / 60.0 "
        "AS ticket_hours, (SELECT SUM(hours) FROM workforce.timesheets WHERE task_category = 'invoice_matching') "
        "AS timesheet_hours")
    assert row["ticket_hours"] < 0.6 * row["timesheet_hours"]
    # ...and the invoice routine repeats as the same ordered steps across many cases.
    cases = sql.run_sql("SELECT COUNT(DISTINCT case_ref) AS n FROM workforce.task_events "
                        "WHERE case_ref LIKE 'INV-%'")
    assert cases[0]["n"] > 100
    people = sql.run_sql("SELECT COUNT(*) AS n FROM workforce.people WHERE synthetic = 'true'")
    assert people[0]["n"] == 12


def test_catalog_types_and_refs():
    tables = {t["table"]: t for t in catalog()}
    assert set(tables) == {"people", "timesheets", "tickets", "email_events", "task_events"}
    assert tables["timesheets"]["ref"] == "workforce.timesheets"
    types = {c["name"]: c["type"] for c in tables["timesheets"]["columns"]}
    assert types["hours"] == "REAL" and types["person_id"] == "TEXT"
    _, _, rows = load_table("tickets")
    assert isinstance(rows[0]["logged_minutes"], int)


def test_sample_sql_runs_portable_queries(sql):
    rows = sql.run_sql("SELECT p.team, SUM(t.hours) AS hours FROM workforce.timesheets t "
                       "JOIN workforce.people p ON p.person_id = t.person_id GROUP BY p.team ORDER BY hours DESC")
    assert {r["team"] for r in rows} == {"Finance", "Operations", "Sales", "Admin"}
    assert len(sql.run_sql("SELECT * FROM workforce.task_events", limit=7)) == 7


@pytest.mark.parametrize("bad, kind", [
    ("DROP TABLE workforce.people", "bad_request"),
    ("SELECT 1; SELECT 2", "bad_request"),
    ("PRAGMA query_only = OFF", "bad_request"),
    ("SELECT nope FROM workforce.people", "sql_error"),
])
def test_sample_sql_is_read_only_and_reports_errors(sql, bad, kind):
    with pytest.raises(AdapterError) as e:
        sql.run_sql(bad)
    assert e.value.type == kind


def test_build_data_source_by_mode(make_settings):
    assert isinstance(build_data_source(make_settings(), sample_mode=True), SampleSQLClient)
    assert isinstance(build_data_source(make_settings(), sample_mode=False), ZetarisSource)


def test_api_serves_tables_for_zetaris(make_settings):
    client = TestClient(create_app(make_settings()))
    body = client.get("/data/workforce/timesheets").json()
    assert body["synthetic"] is True and isinstance(body["rows"][0]["hours"], float)
    missing = client.get("/data/workforce/salaries")
    assert missing.status_code == 404 and missing.json()["error"]["type"] == "not_found"


def test_zetaris_load_plan_is_one_statement_each():
    plan = statements("http://host.docker.internal:8000", views=True)
    assert plan[0][1].startswith("CREATE LIGHTNING DATABASE workforce_api")
    rest = [sql for purpose, sql in plan if purpose.startswith("register")]
    assert len(rest) == 5
    assert 'endpoint "http://host.docker.internal:8000/data/workforce/people"' in rest[0]
    assert all(not sql.rstrip().endswith(";") for _, sql in plan)
    assert any("SCHEMASTORE VIEW workforce.timesheets" in sql for _, sql in plan)
    assert len(verification_queries()) == 5
