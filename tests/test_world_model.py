import json
import threading

import pytest

from app.engines.world_model import (
    STEPS, ContextBuilderService, IngestPipelineService, WorldStoreService, agent_source, context_id,
    entity_id, entity_observation as ent, relationship_id, relationship_observation as rel, zetaris_source,
)
from app.engines.world_model.canonical import name_similarity, pick, reconstruct
from app.tracing import Tracer, verify_chain

TS = zetaris_source("workforce.timesheets", [1])


def world(policy=None, path=":memory:"):
    store = WorldStoreService(path)
    return store, IngestPipelineService(store, policy=policy), ContextBuilderService(store)


def sheet(*rows):
    return zetaris_source("workforce.timesheets", list(rows))


def tickets(*rows):
    return zetaris_source("workforce.tickets", list(rows))


# ------------------------------------------------------------------ stable IDs

def test_ids_are_stable_and_normalized():
    assert entity_id("person", "José  García") == entity_id("Person", "jose garcia")
    assert entity_id("person", "Finance") != entity_id("team", "Finance")
    assert entity_id("person", "x").startswith("ent_") and len(entity_id("person", "x")) == 20
    assert entity_id("person", system="HR", external_id=42) == "person:hr:42"
    assert relationship_id("a", "knows", "b", directed=False) == relationship_id("b", "knows", "a", directed=False)
    assert relationship_id("a", "manages", "b") != relationship_id("b", "manages", "a")
    assert context_id("team", "Finance ") == context_id("TEAM", "finance")


# ------------------------------------------------------------------ ingest basics

def test_ingest_builds_entities_relationships_contexts_and_provenance():
    store, pipeline, _ = world()
    result = pipeline.ingest([
        ent("person", "Ana Silva", source=sheet(1, 2), attributes={"role": "AP clerk"},
            contexts=[("team", "Finance")], observed_at="2026-10-01"),
        ent("task", "Invoice matching", source=sheet(1), tags=["Manual", "repeat"]),
        rel(("person", "Ana Silva"), "works_on", ("task", "Invoice matching"), source=sheet(1),
            weight=6.5, contexts=[("team", "Finance")]),
    ])
    assert result["status"] == "complete" and [s["step"] for s in result["steps"]] == list(STEPS)

    ana = store.resolve("person", "ana silva")
    assert ana["attributes"] == {"role": "AP clerk"}
    assert ana["state"] == "active" and ana["confidence"] == 1.0
    assert ana["first_seen_at"] == "2026-10-01T00:00:00.000Z"
    task = store.resolve("task", "Invoice matching")
    assert task["tags"] == ["manual", "repeat"]

    (edge,) = store.relationships_of(ana["id"])
    assert edge["dst_id"] == task["id"] and edge["properties"]["weight"] == 6.5

    finance = store.get_context(context_id("team", "Finance"))
    assert ana["id"] in finance["entities"] and edge["id"] in finance["relationships"]

    (prov,) = store.provenance(ana["id"])
    assert prov["source"]["table"] == "workforce.timesheets" and prov["source"]["rows"] == [1, 2]
    assert store.subjects_for_source_item("zetaris:workforce.timesheets", 2) == [ana["id"]]
    assert store.stats()["entities_by_type"] == {"person": 1, "task": 1}


def test_provenance_is_required():
    store, pipeline, _ = world()
    result = pipeline.ingest([
        ent("person", "No Source", source={}),
        ent("person", "No Rows", source={"system": "zetaris", "table": "workforce.timesheets", "rows": []}),
        ent("person", "Bad Confidence", source=TS, confidence=1.5),
        ent("person", "Good", source=TS),
    ])
    reasons = [r["reason"] for r in result["rejected"]]
    assert len(reasons) == 3 and "missing provenance" in reasons[0] and "rows" in reasons[1]
    assert result["accepted"] == 1 and store.resolve("person", "Good")
    assert store.resolve("person", "No Source") is None


def test_agent_observations_need_the_agent_name():
    store, pipeline, _ = world()
    bad = pipeline.ingest([ent("automation", "Auto-match", source={"system": "agent"})])
    assert bad["status"] == "empty"
    good = pipeline.ingest([ent("automation", "Auto-match", source=agent_source("PM", run_id="r1"))])
    assert good["accepted"] == 1
    assert store.resolve("automation", "auto-match")["sources"] == ["agent:PM"]


# ------------------------------------------------------------------ idempotency

def test_reingesting_the_same_data_yields_an_empty_diff():
    store, pipeline, _ = world()
    batch = [ent("person", "Ana", source=sheet(1), attributes={"hours": 5}),
             rel(("person", "Ana"), "works_on", ("task", "Recon"), source=sheet(1))]
    first = pipeline.ingest(batch)
    assert first["diff"] and first["new_observations"] == 2
    patches_before = len(store.patches())

    again = pipeline.ingest(list(reversed(batch)))      # same content, different order
    assert again["replayed"] is True and again["diff"] == []

    superset = pipeline.ingest(batch + [ent("person", "Ben", source=sheet(2))])
    assert superset["duplicates"] == 2 and superset["new_observations"] == 1
    assert [d["subject_id"] for d in superset["diff"]] == [entity_id("person", "Ben")]
    assert len(store.patches()) == patches_before + 1
    assert store.resolve("person", "Ana")["version"] == 1


def test_interrupted_batch_resumes(monkeypatch):
    store, pipeline, _ = world()
    batch = [ent("person", "Ana", source=sheet(1)),
             rel(("person", "Ana"), "works_on", ("task", "Recon"), source=sheet(1))]

    calls = {"n": 0}
    original = IngestPipelineService._persist_edges

    def crash_once(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("power cut")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(IngestPipelineService, "_persist_edges", crash_once)
    with pytest.raises(RuntimeError):
        pipeline.ingest(batch)
    assert store.resolve("person", "Ana") is None          # derived steps rolled back
    assert store.stats() == {}                              # aggregates rolled back too
    assert len(store.provenance(entity_id("person", "Ana"))) == 1   # but the log append stuck

    resumed = pipeline.ingest(batch)
    assert resumed["resumed"] is True and resumed["status"] == "complete"
    clean_store, clean_pipeline, _ = world()
    clean_pipeline.ingest(batch)
    for eid in (entity_id("person", "Ana"), entity_id("task", "Recon")):
        a, b = store.get_entity(eid), clean_store.get_entity(eid)
        assert {k: v for k, v in a.items() if k not in ("updated_at", "first_seen_at", "last_seen_at")} == \
               {k: v for k, v in b.items() if k not in ("updated_at", "first_seen_at", "last_seen_at")}


# ------------------------------------------------------------------ canonical reconstruction

def _obs(value, source, conf, at, i):
    return {"id": f"o{i}", "source_key": source, "confidence": conf, "observed_at": at, "seq": i,
            "entity": {"attributes": {"v": value}}}


def test_strategies():
    pairs = [("short", _obs(0, "s1", 0.9, "1", 1)), ("a much longer text", _obs(0, "s2", 0.4, "2", 2)),
             ("mid text", _obs(0, "s3", 0.6, "3", 3))]
    assert pick("latest", pairs) == "mid text"
    assert pick("longest_text", pairs) == "a much longer text"
    assert pick("most_confident", pairs) == "short"
    assert pick("union", [(["a", "b"], None), ("b", None), (["c"], None)]) == ["a", "b", "c"]
    weighted = [(5, _obs(5, "timesheets", 0.6, "1", 1)), (5, _obs(5, "calendar", 0.6, "2", 2)),
                (3, _obs(3, "tickets", 0.9, "3", 3))]
    assert pick("provenance_weighted", weighted) == 5    # two sources at 0.6 beat one at 0.9


def test_conflicts_are_between_sources_not_over_time():
    observations = [_obs(4, "timesheets", 1.0, "2026-10-01", 1), _obs(5, "timesheets", 1.0, "2026-10-02", 2)]
    attributes, conflicts = reconstruct(observations, lambda o: o["entity"]["attributes"], {})
    assert attributes == {"v": 5} and conflicts == []

    observations.append(_obs(3, "tickets", 0.8, "2026-10-03", 3))
    attributes, conflicts = reconstruct(observations, lambda o: o["entity"]["attributes"],
                                        {"v": "most_confident"})
    assert attributes == {"v": 5}
    (conflict,) = conflicts
    assert {c["value"]: c["sources"] for c in conflict["candidates"]} == {5: ["timesheets"], 3: ["tickets"]}


def test_conflicting_sources_surface_in_the_world_with_evidence():
    store, pipeline, _ = world(policy={"task": {"hours_per_week": "provenance_weighted"}})
    pipeline.ingest([
        ent("task", "Invoice matching", source=sheet(7), attributes={"hours_per_week": 6},
            observed_at="2026-10-01"),
        ent("task", "Invoice matching", source=tickets(31), attributes={"hours_per_week": 2},
            confidence=0.7, observed_at="2026-10-02"),
    ])
    task = store.resolve("task", "Invoice matching")
    assert task["attributes"]["hours_per_week"] == 6       # 1.0 support beats 0.7 despite being older
    assert task["confidence"] == 1.0 and task["sources"] == ["zetaris:workforce.tickets",
                                                              "zetaris:workforce.timesheets"]
    (conflict,) = store.conflicts()
    assert conflict["attribute"] == "hours_per_week" and len(conflict["candidates"]) == 2
    assert store.stats()["conflicts"] == 1
    history = store.history(task["id"], "hours_per_week")
    assert [h["value"] for h in history] == [6, 2]


# ------------------------------------------------------------------ resolution

def test_aliases_resolve_later_mentions_to_the_same_entity():
    store, pipeline, _ = world()
    pipeline.ingest([ent("person", "Robert Lee", source=sheet(1), aliases=["Bob Lee"])])
    pipeline.ingest([ent("person", "bob  lee", source=tickets(9), attributes={"team": "Finance"})])
    robert = store.resolve("person", "Robert Lee")
    assert robert["id"] == store.lookup_id("person", "Bob Lee")
    assert robert["mention_count"] == 2 and robert["attributes"] == {"team": "Finance"}
    assert store.stats()["entities_by_type"] == {"person": 1}


def test_similar_names_become_merge_proposals_never_merges():
    store, pipeline, _ = world()
    pipeline.ingest([ent("person", "Jonathan Smith", source=sheet(1)),
                     ent("person", "Jonathon Smith", source=tickets(2)),
                     ent("invoice", "Invoice 101", source=sheet(3)),
                     ent("invoice", "Invoice 102", source=sheet(4))])
    (proposal,) = store.merge_proposals()
    assert {proposal["entity_a"], proposal["entity_b"]} == {entity_id("person", "Jonathan Smith"),
                                                            entity_id("person", "Jonathon Smith")}
    assert store.stats()["entities_by_type"]["person"] == 2   # both still exist
    assert name_similarity("Invoice 101", "Invoice 102") > 0.85  # similar text, but numbers differ


def test_relationship_endpoints_get_stub_entities_until_observed():
    store, pipeline, _ = world()
    pipeline.ingest([rel(("person", "Ana"), "uses", ("tool", "Excel"), source=sheet(1), confidence=0.8)])
    excel = store.resolve("tool", "Excel")
    assert excel["inferred"] is True and excel["state"] == "emerging" and excel["confidence"] == 0.4

    pipeline.ingest([ent("tool", "Excel", source=sheet(2), attributes={"vendor": "Microsoft"})])
    excel = store.resolve("tool", "Excel")
    assert excel["inferred"] is False and excel["attributes"] == {"vendor": "Microsoft"}


# ------------------------------------------------------------------ staleness

def test_snapshot_marks_missing_records_stale_and_back():
    store, pipeline, _ = world()
    source = "zetaris:workforce.timesheets"
    pipeline.ingest([ent("person", "Ana", source=sheet(1)), ent("person", "Ben", source=sheet(2))],
                    snapshot_of=source)
    second = pipeline.ingest([ent("person", "Ana", source=sheet(1))], snapshot_of=source)
    assert second["stale_changes"] == 1
    ben = store.resolve("person", "Ben")
    assert ben["stale"] is True and ben["state"] == "archived"
    assert [e["name"] for e in store.find_entities(types=["person"])] == ["Ana"]
    assert store.stats()["stale"]["entities"] == 1

    pipeline.ingest([ent("person", "Ana", source=sheet(1)), ent("person", "Ben", source=sheet(2))],
                    snapshot_of=source)
    assert store.resolve("person", "Ben")["stale"] is False


# ------------------------------------------------------------------ concurrency + persistence

def test_concurrent_writers_lose_nothing_and_match_a_serial_run(tmp_path):
    batches = [[ent("task", "Invoice matching", source=sheet(i), attributes={"hours": i, f"note{i}": i},
                    observed_at=f"2026-10-{i + 1:02d}")] for i in range(10)]

    store, pipeline, _ = world(path=tmp_path / "parallel.db")
    threads = [threading.Thread(target=pipeline.ingest, args=(b,)) for b in batches]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    serial_store, serial_pipeline, _ = world()
    for b in batches:
        serial_pipeline.ingest(b)

    parallel = store.resolve("task", "Invoice matching")
    serial = serial_store.resolve("task", "Invoice matching")
    assert parallel["mention_count"] == 10
    assert parallel["attributes"] == serial["attributes"] and parallel["attributes"]["hours"] == 9
    assert len(parallel["attributes"]) == 11


def test_store_persists_and_is_schema_stamped(tmp_path):
    path = tmp_path / "world.db"
    store, pipeline, _ = world(path=path)
    pipeline.ingest([ent("person", "Ana", source=sheet(1))])
    assert store.schema_version == 1
    store.close()
    reopened = WorldStoreService(path)
    assert reopened.resolve("person", "Ana")["name"] == "Ana"
    assert reopened.stats()["schema_version"] == 1


def test_ingest_steps_are_traced(tmp_path):
    tracer = Tracer("wm1", tmp_path, stream=None)
    _, pipeline, _ = world()
    pipeline.ingest([ent("person", "Ana", source=sheet(1))], tracer=tracer)
    actions = [json.loads(line)["action"] for line in tracer.path.read_text(encoding="utf-8").splitlines()]
    assert actions == [f"world_model.{s}" for s in STEPS]
    assert verify_chain(tracer.path) is None


# ------------------------------------------------------------------ context builder

def _office():
    store, pipeline, builder = world()
    pipeline.ingest([
        ent("team", "Finance", source=sheet(1)),
        ent("person", "Ana Silva", source=sheet(1), attributes={"role": "AP clerk"}),
        ent("task", "Invoice matching", source=sheet(1), description="Match invoices to purchase orders by hand",
            attributes={"hours_per_week": 6}),
        ent("task", "Expense review", source=sheet(2), attributes={"hours_per_week": 2}),
        ent("tool", "Excel", source=sheet(1)),
        ent("person", "Raj Patel", source=sheet(3), attributes={"role": "Sales lead"}),
        rel(("person", "Ana Silva"), "member_of", ("team", "Finance"), source=sheet(1)),
        rel(("person", "Ana Silva"), "works_on", ("task", "Invoice matching"), source=sheet(1)),
        rel(("task", "Invoice matching"), "uses", ("tool", "Excel"), source=sheet(1)),
    ])
    return store, builder


def test_context_builder_returns_structured_ranked_payload():
    store, builder = _office()
    payload = builder.build("How much time does finance spend matching invoices?", depth=1)
    assert isinstance(payload, dict) and not isinstance(payload["entities"], str)
    names = [e["name"] for e in payload["entities"]]
    assert names[0] == "Invoice matching"                    # two name terms, incl. plural "invoices"
    assert {"Finance", "Ana Silva", "Excel"} <= set(names)    # Finance by name, others via 1 hop
    assert "Raj Patel" not in names and "Expense review" not in names
    assert {r["rel_type"] for r in payload["relationships"]} >= {"works_on", "uses"}
    invoice = payload["entities"][0]
    assert invoice["provenance"][0]["source_key"] == "zetaris:workforce.timesheets"
    trace = payload["trace"]
    assert trace["query_terms"] == ["finance", "invoice", "matching", "spend", "time"]
    assert trace["token_estimate"] <= trace["token_budget"]
    assert any(s["via"].startswith("hop 1") for s in trace["selected"])


def test_context_builder_respects_budget_and_explicit_ids():
    store, builder = _office()
    small = builder.build("invoice finance excel ana", depth=0, token_budget=250)
    assert small["trace"]["dropped_for_budget"]
    assert small["trace"]["token_estimate"] <= 250 or len(small["entities"]) == 1

    raj = entity_id("person", "Raj Patel")
    payload = builder.build(entity_ids=[raj], depth=0, max_entities=1)
    assert [e["id"] for e in payload["entities"]] == [raj]
    assert payload["trace"]["selected"][0]["via"] == "requested"
