"""Generate the SYNTHETIC workforce dataset (no real people, deterministic).

    python scripts/generate_workforce_data.py            # write data/sample/workforce/*.csv
    python scripts/generate_workforce_data.py --check    # verify committed files match the generator

Tables (all synthetic, schema `workforce`):
    people         who works where (names are invented; emails use example.com)
    timesheets     self-reported hours per task category per day
    tickets        ticket-system records with the minutes people logged on them
    email_events   email metadata only (no bodies)
    task_events    application activity log: one row per action, grouped by case_ref

The data contains patterns for the agents to find (it does not contain the
answers): a repeated multi-step invoice routine in task_events, timesheets
that disagree with ticket logs, and inbound vendor invoices driving the work.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "sample" / "workforce"
SEED = 20261022
GENERATOR_VERSION = 2
START = date(2026, 8, 31)        # Monday
WORKDAYS = 30                     # six weeks

FIRST = ["Ana", "Ben", "Chloe", "Dev", "Elif", "Farid", "Grace", "Hugo", "Isla", "Jonah", "Kemi", "Liam",
         "Maya", "Nico"]
LAST = ["Arden", "Brook", "Calder", "Dune", "Ellery", "Fenn", "Gale", "Hollis", "Ives", "Juno", "Kade",
        "Lark", "Moss", "North"]

TEAMS = {
    "Finance": [("AP clerk", 2), ("Finance analyst", 1), ("Finance manager", 1)],
    "Operations": [("Ops coordinator", 2), ("Ops lead", 1)],
    "Sales": [("Account executive", 2), ("Sales ops", 1)],
    "Admin": [("Office admin", 1), ("HR generalist", 1)],
}

# task category -> (weight in a day, tools) per team; weights are relative hours.
TASKS = {
    "Finance": {"invoice_matching": (3.2, "Excel;ERP;Email"), "expense_review": (1.2, "ERP"),
                "vendor_queries": (1.1, "Email;Ticketing"), "month_end_close": (0.9, "ERP;Excel"),
                "reporting": (1.0, "Excel")},
    "Operations": {"order_status_updates": (2.6, "Email;ERP"), "inventory_reconciliation": (1.6, "Excel"),
                   "shipping_exceptions": (1.5, "Ticketing;Email"), "scheduling": (1.4, "Calendar")},
    "Sales": {"crm_data_entry": (2.2, "CRM;Email"), "client_calls": (2.4, "Phone;CRM"),
              "quote_preparation": (1.6, "Excel;Email"), "pipeline_reporting": (0.9, "Excel;CRM")},
    "Admin": {"onboarding_paperwork": (2.0, "Email;PDF"), "timesheet_chasing": (1.3, "Email"),
              "meeting_scheduling": (1.8, "Calendar;Email"), "office_requests": (2.0, "Ticketing")},
}

# Ticket logging habits: the fraction of real time people log in the ticket system.
TICKET_LOGGING = {"invoice_matching": 0.35, "vendor_queries": 0.9, "shipping_exceptions": 0.85,
                  "office_requests": 0.9, "order_status_updates": 0.5}

ROUTINES = {
    "invoice_matching": [("Email", "open_invoice_email", 45), ("PDF", "download_attachment", 30),
                         ("Excel", "open_invoice_tracker", 40), ("ERP", "lookup_purchase_order", 210),
                         ("ERP", "enter_invoice", 330), ("ERP", "match_to_po", 150),
                         ("Email", "reply_vendor_confirmation", 120)],
    "crm_data_entry": [("Email", "open_lead_email", 40), ("CRM", "search_contact", 60),
                       ("CRM", "create_contact", 240), ("CRM", "log_activity", 120)],
    "order_status_updates": [("Email", "open_status_request", 40), ("ERP", "lookup_order", 150),
                             ("Email", "reply_customer_status", 180)],
}
# routine -> (team, role, cases per person per day)
ROUTINE_OWNERS = {"invoice_matching": ("Finance", "AP clerk", 12),
                  "crm_data_entry": ("Sales", "Account executive", 6),
                  "order_status_updates": ("Operations", "Ops coordinator", 8)}


def _workdays():
    d, out = START, []
    while len(out) < WORKDAYS:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _ts(day: date, seconds: int) -> str:
    return (datetime(day.year, day.month, day.day) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")


def generate(seed: int = SEED) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    days = _workdays()

    people, n = [], 0
    for team, roles in TEAMS.items():
        for role, count in roles:
            for _ in range(count):
                first, last = FIRST[n % len(FIRST)], LAST[(n * 5 + 3) % len(LAST)]
                n += 1
                people.append({"person_id": f"P{n:03d}", "name": f"{first} {last}", "team": team, "role": role,
                               "email": f"{first.lower()}.{last.lower()}@example.com", "synthetic": "true"})

    timesheets, tickets, emails, events = [], [], [], []
    for p in people:
        tasks = TASKS[p["team"]]
        for day in days:
            # each person reports 3 task lines a day, ~7.5 hours total
            names = list(tasks)
            weights = [tasks[t][0] for t in names]
            if p["role"] not in ("AP clerk",) and "invoice_matching" in names:
                weights[names.index("invoice_matching")] *= 0.3   # mostly the AP clerks' job
            picked = sorted(set(rng.choices(names, weights=weights, k=3)))
            shares = [tasks[t][0] * rng.uniform(0.7, 1.3) for t in picked]
            total = sum(shares)
            for task, share in zip(picked, shares):
                hours = round(7.5 * share / total * 4) / 4 or 0.25
                timesheets.append({"entry_id": f"T{len(timesheets) + 1:05d}", "person_id": p["person_id"],
                                   "work_date": day.isoformat(), "task_category": task,
                                   "tools": tasks[task][1], "hours": f"{hours:.2f}"})
                if task in TICKET_LOGGING and rng.random() < 0.8:
                    logged = hours * 60 * TICKET_LOGGING[task] * rng.uniform(0.8, 1.2)
                    opened = rng.randint(8 * 3600, 15 * 3600)
                    tickets.append({"ticket_id": f"K{len(tickets) + 1:05d}", "assignee_id": p["person_id"],
                                    "team": p["team"], "category": task, "opened_at": _ts(day, opened),
                                    "closed_at": _ts(day, opened + int(logged * 60 * rng.uniform(1.5, 4))),
                                    "status": "closed", "logged_minutes": str(int(logged))})

    for task, (team, role, per_day) in ROUTINE_OWNERS.items():
        owners = [p for p in people if p["team"] == team and p["role"] == role]
        steps = ROUTINES[task]
        for p in owners:
            for day in days:
                clock = 9 * 3600 + rng.randint(0, 1800)
                for i in range(per_day + rng.randint(-2, 2)):
                    case = f"{task[:3].upper()}-{day.strftime('%m%d')}-{p['person_id'][1:]}-{i + 1}"
                    if task == "invoice_matching":
                        emails.append({"event_id": f"E{len(emails) + 1:05d}", "person_id": p["person_id"],
                                       "sent_at": _ts(day, clock - rng.randint(600, 7200)),
                                       "direction": "inbound", "counterparty": "external_vendor",
                                       "subject_category": "invoice", "has_attachment": "true",
                                       "case_ref": case})
                    for step_index, (app, action, seconds) in enumerate(steps):
                        if rng.random() < 0.05 and 0 < step_index < len(steps) - 1:
                            continue          # a step skipped now and then
                        if rng.random() < 0.08:  # an interruption between steps
                            events.append({"event_id": f"A{len(events) + 1:06d}", "person_id": p["person_id"],
                                           "ts": _ts(day, clock), "app": "Chat", "action": "reply_message",
                                           "case_ref": "", "duration_seconds": str(rng.randint(30, 300))})
                            clock += 240
                        duration = int(seconds * rng.uniform(0.6, 1.6))
                        events.append({"event_id": f"A{len(events) + 1:06d}", "person_id": p["person_id"],
                                       "ts": _ts(day, clock), "app": app, "action": action, "case_ref": case,
                                       "duration_seconds": str(duration)})
                        clock += duration + rng.randint(5, 60)
                    clock += rng.randint(120, 900)

    # background email noise for everyone
    for p in people:
        for day in days:
            for _ in range(rng.randint(3, 8)):
                emails.append({"event_id": f"E{len(emails) + 1:05d}", "person_id": p["person_id"],
                               "sent_at": _ts(day, rng.randint(8 * 3600, 18 * 3600)),
                               "direction": rng.choice(["inbound", "outbound"]),
                               "counterparty": rng.choice(["internal", "internal", "external_customer"]),
                               "subject_category": rng.choice(["meeting", "status", "question", "fyi"]),
                               "has_attachment": rng.choice(["false", "false", "true"]), "case_ref": ""})

    emails.sort(key=lambda e: (e["sent_at"], e["event_id"]))
    events.sort(key=lambda e: (e["ts"], e["event_id"]))
    return {"people": people, "timesheets": timesheets, "tickets": tickets,
            "email_events": emails, "task_events": events}


def to_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def render(seed: int = SEED) -> dict[str, str]:
    tables = generate(seed)
    files = {f"{name}.csv": to_csv(rows) for name, rows in tables.items()}
    manifest = {
        "dataset": "workforce", "synthetic": True, "generator": "scripts/generate_workforce_data.py",
        "generator_version": GENERATOR_VERSION, "seed": seed,
        "period": {"start": _workdays()[0].isoformat(), "end": _workdays()[-1].isoformat(), "workdays": WORKDAYS},
        "note": "Synthetic data. All people, emails and events are invented; emails use the reserved "
                "example.com domain.",
        "tables": {name: {"rows": len(rows), "columns": list(rows[0]),
                          "sha256": hashlib.sha256(files[f"{name}.csv"].encode()).hexdigest()}
                   for name, rows in tables.items()},
    }
    files["manifest.json"] = json.dumps(manifest, indent=2) + "\n"
    return files


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed files differ")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    files = render()
    if args.check:
        stale = [name for name, text in files.items()
                 if not (args.out / name).exists() or (args.out / name).read_text(encoding="utf-8") != text]
        print("up to date" if not stale else f"out of date: {', '.join(stale)}")
        return 1 if stale else 0
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (args.out / name).write_text(text, encoding="utf-8", newline="\n")
    manifest = json.loads(files["manifest.json"])
    print("\n".join(f"{t:13} {m['rows']:6d} rows" for t, m in manifest["tables"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
