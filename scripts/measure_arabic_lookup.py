#!/usr/bin/env python3
"""Naive exact-match vs AgentOffice arabic_* lookup on the synthetic UAE file.

    python scripts/measure_arabic_lookup.py

Prints match rates labelled as measured on data/sample/tasks/uae_staff_lookup.xlsx.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tasks import files, runtime  # noqa: E402
from app.tasks.spec import DEMO_SPECS  # noqa: E402


def naive_match(claims, directory, claim_key, dir_key):
    index = {}
    for r in directory:
        k = r.get(dir_key)
        if k is not None and str(k).strip() != "":
            index[str(k)] = r
    hits = 0
    total = 0
    for r in claims:
        v = r.get(claim_key)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        total += 1
        if str(v) in index:
            hits += 1
    return hits, total


def main() -> int:
    path = ROOT / "data" / "sample" / "tasks" / "uae_staff_lookup.xlsx"
    parsed = files.parse(path.name, path.read_bytes())
    claims = files.sheet(parsed, "Claims")["rows"]
    directory = files.sheet(parsed, "Directory")["rows"]

    id_naive = naive_match(claims, directory, "Staff ID", "id")
    name_naive = naive_match(claims, directory, "Name", "name")

    spec = DEMO_SPECS["uae_staff_lookup"]
    ref = {"directory": directory}
    out = runtime.execute(spec, claims, ref)["rows"]
    id_hits = sum(1 for r in out if r.get("ID Match") == "matched")
    name_hits = sum(1 for r in out if r.get("Name Match") == "matched")
    team_hits = sum(1 for r in out if r.get("Team"))
    n = len(out)

    dates_ok = sum(1 for r in out if isinstance(r.get("Date"), str) and len(r["Date"]) == 10 and r["Date"][4] == "-")
    amounts_ok = sum(1 for r in out if isinstance(r.get("Amount"), (int, float)))

    def pct(hits, total):
        return 0 if total == 0 else round(100 * hits / total)

    print(f"Measured on synthetic file: {path.relative_to(ROOT)}")
    print()
    print(f"Staff ID lookup  naive exact-match: {id_naive[0]}/{id_naive[1]} ({pct(*id_naive)}%)")
    print(f"Staff ID lookup  AgentOffice (arabic_digits): {id_hits}/{n} ({pct(id_hits, n)}%)")
    print(f"Name lookup      naive exact-match: {name_naive[0]}/{name_naive[1]} ({pct(*name_naive)}%)")
    print(f"Name lookup      AgentOffice (arabic_names): {name_hits}/{n} ({pct(name_hits, n)}%)")
    print(f"Team filled      AgentOffice: {team_hits}/{n} ({pct(team_hits, n)}%)")
    print(f"Dates ISO        after format_date (Arabic months + Hijri): {dates_ok}/{n}")
    print(f"Amounts numeric  after parse_amount (د.إ / ر.س / AED): {amounts_ok}/{n}")
    print()
    # Headline figure used in the README (name key is the hard cross-script case).
    print(f"README headline: naive name match {pct(*name_naive)}%, "
          f"AgentOffice name match {pct(name_hits, n)}% "
          f"(measured on this synthetic file).")
    return 0 if name_hits == n and id_hits == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
