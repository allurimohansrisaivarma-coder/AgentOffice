"""The data layer seam: Zetaris in default mode, the same SQL on SQLite in sample mode.

Agents write SQL against `workforce.<table>`. In default mode that runs on
Zetaris (see scripts/load_zetaris.py for how the tables get there), with each
reference rewritten to read the matching REST table (rewrite_for_zetaris
below; the trace records the executed SQL). In sample
mode it runs on an in-memory SQLite copy of the committed synthetic CSVs,
attached under the same `workforce` name, so the SQL is unchanged. Sample mode
is read-only and is always logged as a skipped Zetaris call by the caller.

Keep agent SQL to the portable subset: SELECT, JOIN, WHERE, GROUP BY, HAVING,
ORDER BY, LIMIT, COUNT/SUM/AVG/MIN/MAX, CASE, CAST. No dialect-specific
date functions; dates are ISO strings, so substr(work_date, 1, 7) gives a month.
`/` gives a decimal on Zetaris even between whole numbers (8 / 7 = 1.142857),
so wrap any whole-number result in CAST(... AS INTEGER). For a week, group by
the month plus CAST((CAST(substr(work_date, 9, 2) AS INTEGER) - 1) / 7 AS INTEGER) + 1,
the week of the month (1 to 5).
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional, Protocol

from app.adapters import zetaris
from app.adapters.http import AdapterError
from app.adapters.zetaris import split_statements
from app.config import ROOT, Settings

DATA_DIR = ROOT / "data" / "sample" / "workforce"
SCHEMA = "workforce"
PORTABLE_SQL_NOTE = __doc__.split("Keep agent SQL")[1].strip()


class DataSource(Protocol):
    name: str

    def run_sql(self, sql: str, limit: int = 100) -> list[dict]: ...


def load_manifest(data_dir: Path = DATA_DIR) -> dict:
    return json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))


def _column_type(values: list[str]) -> str:
    present = [v for v in values if v != ""]
    if not present:
        return "TEXT"
    for kind, cast in (("INTEGER", int), ("REAL", float)):
        try:
            for v in present:
                cast(v)
            return kind
        except ValueError:
            continue
    return "TEXT"


def load_table(name: str, data_dir: Path = DATA_DIR) -> tuple[list[str], dict[str, str], list[dict]]:
    """(columns, {column: SQL type}, rows with numbers as numbers) for one CSV table."""
    path = data_dir / f"{name}.csv"
    with path.open(encoding="utf-8", newline="") as f:
        raw = list(csv.DictReader(f))
    columns = list(raw[0]) if raw else []
    types = {c: _column_type([r[c] for r in raw]) for c in columns}
    cast = {"INTEGER": int, "REAL": float, "TEXT": str}
    rows = [{c: (cast[types[c]](r[c]) if r[c] != "" else None) for c in columns} for r in raw]
    return columns, types, rows


PERSONAL_COLUMNS = {"name", "email"}   # never listed: no personal data in prompts


def catalog(data_dir: Path = DATA_DIR, max_values: int = 20) -> list[dict]:
    """What the agents can discover: table refs, typed columns, row counts, and the exact
    values of small categorical text columns, so model-written filters match the data
    (e.g. role = 'Ops coordinator', not a guessed 'Operations Coordinator')."""
    manifest = load_manifest(data_dir)
    out = []
    for name, meta in manifest["tables"].items():
        _, types, rows = load_table(name, data_dir)
        columns = []
        for c, t in types.items():
            col = {"name": c, "type": t}
            if t == "TEXT" and c not in PERSONAL_COLUMNS and not c.endswith("_id") and c != "case_ref":
                values = sorted({r[c] for r in rows if r[c] is not None})
                if 0 < len(values) <= max_values:
                    col["values"] = values
            columns.append(col)
        out.append({"table": name, "ref": f"{SCHEMA}.{name}", "rows": meta["rows"], "columns": columns,
                    "synthetic": manifest["synthetic"]})
    return out


# Uploaded spreadsheets, one row per cell (app/tasks/files.py), served to Zetaris as a REST table too.
UPLOAD_CELLS = "upload_cells"
UPLOAD_CELLS_TYPES = {"upload_id": "TEXT", "sheet_name": "TEXT", "row_no": "INTEGER", "col_name": "TEXT",
                      "cell_value": "TEXT", "value_type": "TEXT"}


class SampleSQLClient:
    """Sample-mode stand-in for Zetaris over the synthetic CSVs (and upload cells). SELECT/WITH only."""

    name = "sample-sqlite"

    def __init__(self, data_dir: Path = DATA_DIR, upload_cells: Optional[list[dict]] = None):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.execute(f"ATTACH DATABASE ':memory:' AS {SCHEMA}")
        tables = []
        for name in load_manifest(data_dir)["tables"]:
            columns, types, rows = load_table(name, data_dir)
            tables.append((name, columns, types, rows))
        if upload_cells is not None:
            tables.append((UPLOAD_CELLS, list(UPLOAD_CELLS_TYPES), UPLOAD_CELLS_TYPES, upload_cells))
        for name, columns, types, rows in tables:
            cols_sql = ", ".join(f'"{c}" {types[c]}' for c in columns)
            self._conn.execute(f'CREATE TABLE {SCHEMA}."{name}" ({cols_sql})')
            self._conn.executemany(
                f'INSERT INTO {SCHEMA}."{name}" VALUES ({", ".join("?" * len(columns))})',
                [tuple(r[c] for c in columns) for r in rows])
        self._conn.commit()
        self._conn.execute("PRAGMA query_only = ON")

    def run_sql(self, sql: str, limit: int = 100) -> list[dict]:
        statements = split_statements(sql)
        if len(statements) != 1:
            raise AdapterError("sample_sql", "bad_request",
                               f"send exactly one SQL statement per call, got {len(statements)}", recoverable=False)
        statement = statements[0]
        if not statement.lstrip().lower().startswith(("select", "with")):
            raise AdapterError("sample_sql", "bad_request", "sample mode is read-only: SELECT or WITH only",
                               recoverable=False)
        with self._lock:
            try:
                cursor = self._conn.execute(statement)
                columns = [d[0] for d in cursor.description or []]
                rows = cursor.fetchmany(limit)
            except sqlite3.Error as e:
                raise AdapterError("sample_sql", "sql_error", str(e), recoverable=False) from None
        return [dict(zip(columns, row)) for row in rows]

    def close(self) -> None:
        self._conn.close()


LIGHTNING_DB = "workforce_api"
_SQL_WORDS = {"where", "group", "order", "join", "left", "right", "inner", "outer", "full", "cross", "natural",
              "on", "using", "limit", "union", "intersect", "except", "having", "lateral", "window", "select"}
_TABLE_REF = re.compile(r"\bworkforce\.([a-z_][a-z0-9_]*)\b(\s+(?:as\s+)?([a-z_][a-z0-9_]*))?", re.I)


def rewrite_for_zetaris(sql: str, tables: set[str]) -> str:
    """Agents write `workforce.<table>`; in Zetaris each table is a REST table holding
    {"rows": [...]}, so read it as (SELECT r.* FROM workforce_api.<table> LATERAL VIEW
    explode(rows) AS r). Aliases are kept; an unaliased table gets its own name as alias,
    so `timesheets.hours` still resolves. No database objects are created."""
    def replace(m: re.Match) -> str:
        table = m.group(1).lower()
        if table not in tables:
            return m.group(0)
        sub = f"(SELECT r.* FROM {LIGHTNING_DB}.{table} LATERAL VIEW explode(rows) AS r)"
        alias = m.group(3)
        if alias and alias.lower() not in _SQL_WORDS:
            return f"{sub} AS {alias}"
        return f"{sub} AS {table}{m.group(2) or ''}"
    return _TABLE_REF.sub(replace, sql)


class ZetarisSource:
    name = "zetaris"

    def __init__(self, client: zetaris.ZetarisClient, tables: Optional[set[str]] = None):
        self._client = client
        self._tables = tables if tables is not None else set(load_manifest()["tables"]) | {UPLOAD_CELLS}
        self.last_executed: Optional[str] = None

    def run_sql(self, sql: str, limit: int = 100) -> list[dict]:
        self.last_executed = rewrite_for_zetaris(sql, self._tables)
        return self._client.run_sql(self.last_executed, limit=limit)

    def close(self) -> None:
        self._client.close()


def build_data_source(settings: Settings, sample_mode: bool, transport: Optional[Any] = None,
                      upload_cells: Optional[list[dict]] = None) -> DataSource:
    """In sample mode `upload_cells` fills the sample upload_cells table; Zetaris reads the live endpoint."""
    if sample_mode:
        return SampleSQLClient(upload_cells=upload_cells)
    return ZetarisSource(zetaris.from_settings(settings, transport=transport))
