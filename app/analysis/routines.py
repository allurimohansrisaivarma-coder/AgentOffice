"""Evidence the agents reason over, computed from SQL rows (never hand-written).

Routine mining: group activity events by case, order them in time, and cluster
cases by (first action, last action) so a skipped middle step or an
interruption (an event with no case) does not split a routine. The most common
sequence in a cluster is its canonical routine.

Time evidence: hands-on time from the activity log, self-reported time from
timesheets, and time logged in the ticket system, each per week.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Callable

QueryFn = Callable[[str, str], list]   # (sql, purpose) -> rows

WRITE_VERBS = {"enter", "create", "update", "match", "reply", "send", "post", "pay", "delete", "approve"}
RECORD_SYSTEMS = {"ERP": "financial records", "CRM": "customer records"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2}


def _risk(steps: list[tuple[str, str]]) -> tuple[str, list[str]]:
    reasons = []
    level = "low"
    for app, action in steps:
        verb = action.split("_")[0]
        if verb not in WRITE_VERBS:
            continue
        if app in RECORD_SYSTEMS:
            reasons.append(f"{action} writes {RECORD_SYSTEMS[app]} in {app}")
            level = "high" if app == "ERP" else max(level, "medium", key=["low", "medium", "high"].index)
        elif app == "Email" and verb in ("reply", "send"):
            reasons.append(f"{action} sends email outside the team")
            level = max(level, "medium", key=["low", "medium", "high"].index)
    return level, reasons


def gather(query: QueryFn, *, min_instances: int = 10) -> dict:
    events = query("SELECT event_id, case_ref, person_id, ts, app, action, duration_seconds "
                   "FROM workforce.task_events ORDER BY ts", "activity log for routine mining")
    timesheets = query("SELECT task_category, SUM(hours) AS hours, COUNT(DISTINCT person_id) AS people, "
                       "COUNT(DISTINCT work_date) AS days FROM workforce.timesheets GROUP BY task_category",
                       "self-reported hours per task category")
    tickets = query("SELECT category, SUM(logged_minutes) AS minutes, COUNT(*) AS tickets "
                    "FROM workforce.tickets GROUP BY category", "time logged in the ticket system")

    days = {str(e["ts"])[:10] for e in events}
    weeks = max(len(days) / 5, 1e-9)
    ts_days = max((int(r["days"] or 0) for r in timesheets), default=0)
    ts_weeks = max(ts_days / 5, 1e-9)

    cases: dict[str, list[dict]] = defaultdict(list)
    interruptions = 0
    for e in events:
        if e.get("case_ref"):
            cases[e["case_ref"]].append(e)
        else:
            interruptions += 1

    clusters: dict[tuple, list[str]] = defaultdict(list)
    for case, evs in cases.items():
        evs.sort(key=lambda e: str(e["ts"]))
        clusters[(evs[0]["app"], evs[0]["action"], evs[-1]["app"], evs[-1]["action"])].append(case)

    category_hours = {r["task_category"]: float(r["hours"] or 0) for r in timesheets}
    ticket_hours = {r["category"]: float(r["minutes"] or 0) / 60 for r in tickets}

    routines = []
    for _, case_ids in clusters.items():
        if len(case_ids) < min_instances:
            continue
        sequences = Counter(tuple((e["app"], e["action"]) for e in cases[c]) for c in case_ids)
        canonical, exact = sequences.most_common(1)[0]
        seconds = sum(int(e["duration_seconds"] or 0) for c in case_ids for e in cases[c])
        people = {e["person_id"] for c in case_ids for e in cases[c]}
        step_tokens = set().union(*(_tokens(f"{app} {action}") for app, action in canonical))
        overlaps = {cat: len(step_tokens & _tokens(cat)) for cat in category_hours}
        best = max(overlaps.items(), key=lambda kv: (kv[1], category_hours[kv[0]]), default=(None, 0))
        category = best[0] if best[1] > 0 else None
        risk, risk_reasons = _risk(list(canonical))
        routines.append({
            "routine": category or "_".join(a for _, a in (canonical[0], canonical[-1])),
            "timesheet_category": category,
            "steps": [f"{app}.{action}" for app, action in canonical],
            "instances": len(case_ids),
            "instances_per_week": round(len(case_ids) / weeks, 1),
            "people": len(people),
            "active_hours_per_week": round(seconds / 3600 / weeks, 2),
            "avg_minutes_per_instance": round(seconds / 60 / len(case_ids), 1),
            "variant_share": round(1 - exact / len(case_ids), 3),
            "reported_hours_per_week": round(category_hours[category] / ts_weeks, 2) if category else None,
            "ticket_logged_hours_per_week": (round(ticket_hours[category] / ts_weeks, 2)
                                             if category in ticket_hours else None),
            "risk": risk, "risk_reasons": risk_reasons,
            "example_cases": sorted(case_ids)[:3],
            "evidence_rows": sorted(e["event_id"] for c in sorted(case_ids)[:3] for e in cases[c]),
        })
    routines.sort(key=lambda r: -r["active_hours_per_week"])
    return {
        "weeks": round(weeks, 2), "events": len(events), "cases": len(cases), "interruptions": interruptions,
        "routines": routines,
        "timesheet_hours_per_week": {k: round(v / ts_weeks, 2) for k, v in sorted(category_hours.items())},
        "ticket_hours_per_week": {k: round(v / ts_weeks, 2) for k, v in sorted(ticket_hours.items())},
    }
