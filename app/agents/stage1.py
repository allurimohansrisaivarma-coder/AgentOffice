"""Stage 1, Discover (Track 1, fragmented intelligence): where does the team's time go?

    Source Mapper   picks the tables and join keys for the question, probes each one
    Reconciler      links the same task across timesheets, tickets and the activity log
                    and writes people, teams, tasks, tools and their links to the World Model
    Conflict Critic reads the conflicts the World Model surfaces (sources that disagree
                    about the same fact) and judges which source to trust for what

People are recorded by person_id only: names never enter prompts or traces.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.agents.base import OFFLINE_MODEL, Agent, AgentError
from app.engines.world_model import entity_observation, relationship_observation

# How much each source is trusted as a measure of time spent (World Model confidence).
SOURCE_TRUST = {"activity": 0.9, "timesheets": 0.6, "tickets": 0.5}


def _ref_ok(ref: Any, columns: dict[str, set]) -> bool:
    if not isinstance(ref, str) or "." not in ref:
        return False
    table, column = ref.split(".", 1)
    return column in columns.get(table, set())


class SourceMapper(Agent):
    name = "SourceMapperAgent"

    SYSTEM = ("You are the Source Mapper agent in AgentOffice. Given a question about how an office team spends its "
              "time and the catalog of available tables, choose the tables needed to answer it and the join keys "
              "between them. Use only tables and columns from the catalog. Reply with only JSON: {\"tables\": "
              "[<table>], \"joins\": [{\"left\": \"table.column\", \"right\": \"table.column\"}], \"reason\": \"...\"}.")

    def map(self, query: str, catalog: list[dict]) -> dict:
        columns = {t["table"]: {c["name"] for c in t["columns"]} for t in catalog}

        def validate(obj: Any) -> list[str]:
            if not isinstance(obj, dict) or not isinstance(obj.get("tables"), list) or not obj["tables"]:
                return ["'tables' must be a non-empty list"]
            errors = [f"unknown table {t!r}" for t in obj["tables"] if t not in columns]
            for j in obj.get("joins", []):
                if not isinstance(j, dict) or not _ref_ok(j.get("left"), columns) or not _ref_ok(j.get("right"),
                                                                                                    columns):
                    errors.append(f"join {j} must use existing table.column on both sides")
            return errors

        if self.offline:
            tables = [t["table"] for t in catalog]
            joins = []
            for i, a in enumerate(tables):
                for b in tables[i + 1:]:
                    for col in sorted(columns[a] & columns[b]):
                        if col.endswith("_id") or col == "case_ref":
                            joins.append({"left": f"{a}.{col}", "right": f"{b}.{col}"})
            mapping = {"tables": tables, "joins": joins,
                       "reason": "offline: every catalog table, joined on shared *_id / case_ref columns"}
            self.log("map_sources", model=OFFLINE_MODEL, output_summary=mapping)
        else:
            mapping = self.chat_json(self.SYSTEM, f"Question: {query}\n\nCatalog:\n{json.dumps(catalog, indent=1)}",
                                     action="map_sources", validate=validate, target_agent="ReconcilerAgent")

        probes = {}
        for table in mapping["tables"]:
            rows = self.query(f"SELECT COUNT(*) AS n FROM workforce.{table}", f"probe {table}")
            probes[table] = int(next(iter(rows[0].values()))) if rows else 0
        empty = [t for t, n in probes.items() if n == 0]
        if empty:
            self.log("probe_sources", status="warning", output_summary=f"empty or unreachable tables: {empty}")
        self.remember([entity_observation(
            "data_source", f"workforce.{t}", source=self.data_source(f"workforce.{t}", ["*"]),
            attributes={"rows": n, "columns": sorted(columns[t])}) for t, n in probes.items()],
            "mapped data sources")
        return {**mapping, "row_counts": probes}


class Reconciler(Agent):
    name = "ReconcilerAgent"

    SYSTEM = ("You are the Reconciler agent in AgentOffice. The same unit of work is named in several systems: "
              "timesheet task categories, ticket categories and routines mined from the activity log. Link names "
              "that refer to the same task. Only link names that appear in the lists; leave a name unlinked rather "
              "than guess. Reply with only JSON: {\"links\": [{\"task\": <timesheet category>, \"ticket_category\": "
              "<name or null>, \"routine\": <name or null>, \"confidence\": 0..1}]}.")

    def _links(self, categories: list[str], ticket_cats: list[str], routines: list[str]) -> list[dict]:
        def validate(obj: Any) -> list[str]:
            if not isinstance(obj, dict) or not isinstance(obj.get("links"), list):
                return ["reply must be {\"links\": [...]}"]
            errors = []
            for i, link in enumerate(obj["links"]):
                if not isinstance(link, dict) or link.get("task") not in categories:
                    errors.append(f"links[{i}].task must be one of the timesheet categories")
                    continue
                if link.get("ticket_category") not in ticket_cats + [None]:
                    errors.append(f"links[{i}].ticket_category must be a ticket category or null")
                if link.get("routine") not in routines + [None]:
                    errors.append(f"links[{i}].routine must be a mined routine or null")
            return errors

        if self.offline:
            links = [{"task": c, "ticket_category": c if c in ticket_cats else None,
                      "routine": c if c in routines else None, "confidence": 1.0} for c in categories]
            self.log("link_entities", model=OFFLINE_MODEL, output_summary="offline: exact name matches only")
            return links
        obj = self.chat_json(self.SYSTEM, json.dumps({"timesheet_categories": categories,
                                                      "ticket_categories": ticket_cats,
                                                      "mined_routines": routines}, indent=1),
                             action="link_entities", validate=validate, target_agent="WorldModel")
        return obj["links"]

    def reconcile(self, evidence: dict) -> dict:
        people = self.query("SELECT person_id, team, role FROM workforce.people", "people and teams")
        work = self.query("SELECT person_id, task_category, SUM(hours) AS hours FROM workforce.timesheets "
                          "GROUP BY person_id, task_category", "who works on which task")
        tools = self.query("SELECT task_category, tools FROM workforce.timesheets GROUP BY task_category, tools",
                           "tools per task")
        weeks = evidence["weeks"]
        ts_hours = evidence["timesheet_hours_per_week"]
        ticket_hours = evidence["ticket_hours_per_week"]
        routines = {r["routine"]: r for r in evidence["routines"]}
        links = self._links(sorted(ts_hours), sorted(ticket_hours), sorted(routines))

        src = lambda table, rows=("*",), q=None: {**self.data_source(f"workforce.{table}", list(rows)), "query": q}
        obs = []
        for p in people:
            obs.append(entity_observation("person", p["person_id"], source=src("people", [p["person_id"]]),
                                          attributes={"team": p["team"], "role": p["role"]},
                                          contexts=[("team", p["team"])]))
            obs.append(entity_observation("team", p["team"], source=src("people", [p["person_id"]])))
            obs.append(relationship_observation(("person", p["person_id"]), "member_of", ("team", p["team"]),
                                                source=src("people", [p["person_id"]])))
        team_of = {p["person_id"]: p["team"] for p in people}
        for w in work:
            obs.append(relationship_observation(
                ("person", w["person_id"]), "works_on", ("task", w["task_category"]),
                source=src("timesheets", ["*"], "SUM(hours) GROUP BY person_id, task_category"),
                weight=round(float(w["hours"]) / weeks, 2), contexts=[("team", team_of.get(w["person_id"], "?"))]))
        for t in tools:
            for tool in str(t["tools"] or "").split(";"):
                if tool.strip():
                    obs.append(relationship_observation(("task", t["task_category"]), "uses", ("tool", tool.strip()),
                                                        source=src("timesheets", ["*"], "tools per task_category")))
        linked = 0
        for link in links:
            task = link["task"]
            obs.append(entity_observation(
                "task", task, source=src("timesheets", ["*"], "SUM(hours) per task_category"),
                attributes={"hours_per_week": ts_hours[task]}, confidence=SOURCE_TRUST["timesheets"],
                tags=["self-reported"]))
            if link.get("ticket_category"):
                linked += 1
                obs.append(entity_observation(
                    "task", task, aliases=[link["ticket_category"]] if link["ticket_category"] != task else [],
                    source=src("tickets", ["*"], "SUM(logged_minutes) per category"),
                    attributes={"hours_per_week": ticket_hours[link["ticket_category"]]},
                    confidence=SOURCE_TRUST["tickets"]))
            if link.get("routine"):
                linked += 1
                r = routines[link["routine"]]
                obs.append(entity_observation(
                    "task", task, aliases=[link["routine"]] if link["routine"] != task else [],
                    source=src("task_events", r["evidence_rows"]),
                    attributes={"hours_per_week": r["active_hours_per_week"],
                                "runs_per_week": r["instances_per_week"]},
                    confidence=SOURCE_TRUST["activity"]))
                obs.append(relationship_observation(("routine", link["routine"]), "instance_of", ("task", task),
                                                    source=self.source()))
        result = self.remember(obs, "reconciled people, teams, tasks and tools")
        return {"links": links, "cross_source_links": linked, "people": len(people),
                "observations": result.get("accepted", 0)}


class ConflictCritic(Agent):
    name = "ConflictCriticAgent"

    SYSTEM = ("You are the Conflict Critic agent in AgentOffice. Several systems disagree about how many hours a "
              "week go into the same task. For each conflict, say which source to trust for sizing an automation "
              "and why, in one or two sentences grounded in what each source measures (activity log = measured "
              "hands-on time; timesheets = self-reported; tickets = time people chose to log). Reply with only JSON: "
              "{\"assessments\": [{\"task\": ..., \"trusted_source\": <one of the candidate sources>, "
              "\"explanation\": ...}]}.")

    @staticmethod
    def _kind(source_key: str) -> str:
        return next((k for k in SOURCE_TRUST if k in source_key or (k == "activity" and "task_events" in source_key)),
                    source_key)

    def review(self, min_ratio: float = 1.5) -> dict:
        conflicts = []
        for c in self.ctx.store.conflicts(types=["task"]):
            if c["attribute"] != "hours_per_week":
                continue
            values = {s: cand["value"] for cand in c["candidates"] for s in cand["sources"]}
            numbers = [v for v in values.values() if isinstance(v, (int, float))]
            if len(numbers) < 2 or min(numbers) <= 0:
                continue
            ratio = max(numbers) / min(numbers)
            if ratio >= min_ratio:
                conflicts.append({"task": c["name"], "values": values, "ratio": round(ratio, 2),
                                  "world_model_choice": c["chosen"]})
        conflicts.sort(key=lambda c: -c["ratio"])
        if not conflicts:
            self.log("critique_sources", status="approved", output_summary="no sources disagree beyond "
                                                                           f"{min_ratio}x")
            return {"conflicts": [], "assessments": []}

        names = {c["task"] for c in conflicts}

        def validate(obj: Any) -> list[str]:
            if not isinstance(obj, dict) or not isinstance(obj.get("assessments"), list):
                return ["reply must be {\"assessments\": [...]}"]
            errors = []
            for i, a in enumerate(obj["assessments"]):
                c = next((c for c in conflicts if isinstance(a, dict) and c["task"] == a.get("task")), None)
                if c is None:
                    errors.append(f"assessments[{i}].task must be one of {sorted(names)}")
                elif a.get("trusted_source") not in c["values"]:
                    errors.append(f"assessments[{i}].trusted_source must be one of {sorted(c['values'])}")
            return errors

        if self.offline:
            assessments = []
            for c in conflicts:
                trusted = next((s for s in c["values"] if self._kind(s) == "activity"), max(
                    c["values"], key=lambda s: SOURCE_TRUST.get(self._kind(s), 0)))
                parts = ", ".join(f"{self._kind(s)} {v:g} h/week" for s, v in sorted(c["values"].items()))
                assessments.append({"task": c["task"], "trusted_source": trusted, "explanation": (
                    f"Sources differ {c['ratio']:g}x ({parts}). The activity log measures hands-on time, so it sizes "
                    "the automation; timesheets are self-reported and tickets are logged selectively.")})
            self.log("critique_sources", model=OFFLINE_MODEL, status="needs_attention", output_summary=assessments,
                     target_agent="ResearchPlannerAgent")
        else:
            obj = self.chat_json(self.SYSTEM, json.dumps(conflicts, indent=1), action="critique_sources",
                                 validate=validate, target_agent="ResearchPlannerAgent")
            assessments = obj["assessments"]

        self.remember([
            entity_observation("data_quality_issue", f"{a['task']} hours disagree across sources",
                               source=self.source(),
                               attributes={"trusted_source": a["trusted_source"], "explanation": a["explanation"],
                                           "ratio": next(c["ratio"] for c in conflicts if c["task"] == a["task"])})
            for a in assessments
        ] + [relationship_observation(("data_quality_issue", f"{a['task']} hours disagree across sources"), "about",
                                      ("task", a["task"]), source=self.source()) for a in assessments],
            "source conflicts and which source to trust")
        return {"conflicts": conflicts, "assessments": assessments}


TEAM_WORDS = {"ops": "Operations", "operation": "Operations", "accounting": "Finance", "accounts": "Finance",
              "sales": "Sales", "admin": "Admin", "hr": "Admin"}


def team_from_query(query: str, teams: list[str]) -> Optional[str]:
    """The team a question is about, if it names one ("ops" counts as Operations)."""
    words = set(re.findall(r"[a-z]+", query.lower()))
    direct = next((t for t in teams if t.lower() in words), None)
    if direct:
        return direct
    return next((TEAM_WORDS[w] for w in sorted(words) if w in TEAM_WORDS and TEAM_WORDS[w] in teams), None)
