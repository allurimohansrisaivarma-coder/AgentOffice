"""Register the synthetic workforce tables in Zetaris as Lightning REST tables.

    python scripts/load_zetaris.py                      # dry run: print every statement, run nothing
    python scripts/load_zetaris.py --apply              # run them one at a time, then verify row counts
    python scripts/load_zetaris.py --verify             # only re-check the row counts (read-only)

Zetaris cannot read files on this machine and has no INSERT for data entry,
so the AgentOffice API serves each table as JSON at GET /data/workforce/<table>
({"rows": [...]}, numbers as JSON numbers) and Zetaris pulls it as a REST
table. The API must be running and reachable from the Zetaris container;
on Docker Desktop that is http://host.docker.internal:<PORT> (override with
--public-url or AGENTOFFICE_PUBLIC_URL).

Statements follow the Zetaris getting-started SQL reference: one statement per
call, no trailing semicolon. CREATE statements need an administrator key.
No views are needed: the data adapter (app/data.py) rewrites the agents'
`workforce.<table>` into a read of the REST table at query time.

Verified on a local Zetaris 2.4.3 install (23 Oct 2026): all 6 statements
succeed and every row count matches the generator's manifest.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.http import AdapterError  # noqa: E402
from app.adapters.zetaris import from_settings  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.data import LIGHTNING_DB, catalog  # noqa: E402


def statements(public_url: str) -> list[tuple[str, str]]:
    """(purpose, SQL) pairs, in the order they must run."""
    base = public_url.rstrip("/")
    out = [("create database",
            f'CREATE LIGHTNING DATABASE {LIGHTNING_DB} DESCRIBE BY "AgentOffice synthetic workforce data"')]
    tables = catalog()
    for t in tables:
        out.append((f"register {t['table']}",
                     f"CREATE LIGHTNING REST TABLE {t['table']} FROM {LIGHTNING_DB} REQUEST (\n"
                     f'  endpoint "{base}/data/workforce/{t["table"]}",\n'
                     f'  method "GET",\n'
                     f'  http_encoding "URLENCODED",\n'
                     f'  response_type "JSON",\n'
                     f'  insecure_ssl "false"\n'
                     f") HEADER () BODY ()"))
    return out


def verification_queries() -> list[tuple[str, str, int]]:
    return [(t["table"], f"SELECT COUNT(*) AS n FROM {LIGHTNING_DB}.{t['table']} LATERAL VIEW explode(rows) AS r",
             t["rows"]) for t in catalog()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Register the synthetic workforce tables in Zetaris.")
    parser.add_argument("--apply", action="store_true", help="run the statements (default: dry run)")
    parser.add_argument("--verify", action="store_true", help="only re-check row counts (read-only)")
    parser.add_argument("--public-url", default=os.environ.get("AGENTOFFICE_PUBLIC_URL"),
                        help="URL the Zetaris container uses to reach this API")
    args = parser.parse_args(argv)

    settings = load_settings()
    public_url = args.public_url or f"http://host.docker.internal:{settings.port}"
    plan = [] if args.verify else statements(public_url)

    if not args.apply and not args.verify:
        print(f"DRY RUN: {len(plan)} statements (nothing sent). Re-run with --apply to execute.\n")
        for purpose, sql in plan:
            print(f"-- {purpose}\n{sql}\n")
        return 0

    try:
        client = from_settings(settings)
    except AdapterError as e:
        print(f"Zetaris not configured: {e.message}")
        return 1
    failures = 0
    for purpose, sql in plan:
        try:
            client.run_sql(sql)
            print(f"OK    {purpose}")
        except AdapterError as e:
            failures += 1
            print(f"FAIL  {purpose}: {e.to_dict()['type']}: {e.message}")
    for table, sql, expected in verification_queries():
        try:
            rows = client.run_sql(sql)
            got = next(iter(rows[0].values())) if rows and isinstance(rows[0], dict) else rows
            ok = got == expected
            failures += not ok
            print(f"{'OK  ' if ok else 'FAIL'}  verify {table}: {got} rows (expected {expected})")
        except AdapterError as e:
            failures += 1
            print(f"FAIL  verify {table}: {e.message}")
    client.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
