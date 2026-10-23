"""Test Connection for the two sponsor services, reported separately.

    python scripts/check_connections.py                  # both services
    python scripts/check_connections.py --only zetaris   # or: --only meterless
    python scripts/check_connections.py --json           # machine-readable

Zetaris: `SELECT 1`, then `SHOW LIGHTNING DATABASES` (both read-only).
Meterless Nightshift: GET /models decides online / unauthorized / offline,
then one short chat call reports which model actually answered (/healthz OK
does not prove a model is connected). --detect scans the standard ports first.

Every check is also written to logs/trace-<id>.jsonl. Secrets are never
printed. Exit code is 0 only if every check that ran passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.adapters import meterless, zetaris  # noqa: E402
from app.adapters.http import AdapterError  # noqa: E402
from app.config import Settings, load_settings  # noqa: E402
from app.tracing import Tracer, new_run_id  # noqa: E402

CHAT_PROMPT = "Reply with one sentence confirming this connection works."


@dataclass
class Check:
    service: str
    name: str
    status: str            # "pass" | "fail" | "skip"
    detail: str
    elapsed_s: float = 0.0
    model: Optional[str] = None


def _short(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def _run(tracer: Tracer, service: str, name: str,
         fn: Callable[[], tuple[str, Optional[str]]]) -> Check:
    started = time.monotonic()
    try:
        detail, model = fn()
        check = Check(service, name, "pass", detail, model=model)
    except AdapterError as e:
        check = Check(service, name, "fail", f"{e.to_dict()['type']}: {e.message}")
    except Exception as e:  # a bug in the check itself must still be reported, not crash
        check = Check(service, name, "fail", f"{type(e).__name__}: {e}")
    check.elapsed_s = round(time.monotonic() - started, 3)
    check.detail = tracer.mask(check.detail)
    tracer.event("ConnectionCheck", f"{service}.{name}",
                 status="ok" if check.status == "pass" else "error",
                 output_summary=check.detail, model=check.model,
                 service=service, elapsed_s=check.elapsed_s)
    return check


def _skip_rest(service: str, names: list[str], reason: str) -> list[Check]:
    return [Check(service, n, "skip", reason) for n in names]


def check_zetaris(settings: Settings, tracer: Tracer,
                  transport: Optional[httpx.BaseTransport] = None) -> list[Check]:
    try:
        client = zetaris.from_settings(settings, transport=transport)
    except AdapterError as e:
        return [Check("zetaris", "config", "fail", f"not configured: {e.message}")]

    def select_1():
        rows = client.run_sql("SELECT 1 AS ok")
        return f"{len(rows)} row(s): {_short(rows[:1])}", None

    def show_databases():
        rows = client.run_sql("SHOW LIGHTNING DATABASES")
        return f"{len(rows)} database(s): {_short(rows[:5])}", None

    try:
        results = [_run(tracer, "zetaris", "select_1", select_1)]
        if results[0].status != "pass":
            return results + _skip_rest("zetaris", ["show_databases"], "skipped: select_1 failed")
        results.append(_run(tracer, "zetaris", "show_databases", show_databases))
        return results
    finally:
        client.close()


def detect_meterless(settings: Settings, tracer: Tracer,
                     transport: Optional[httpx.BaseTransport] = None) -> tuple[Check, Settings]:
    """Auto Detect: scan the standard ports; use the hit if no base URL is configured."""
    def detect():
        found = meterless.auto_detect(settings.meterless_runtime_token, app_name=settings.meterless_app,
                                      transport=transport)
        if found is None:
            raise AdapterError("meterless", "not_found",
                               f"no runtime answered on ports {', '.join(map(str, meterless.DETECT_PORTS))}",
                               recoverable=True)
        return f"found {found['base_url']} ({found['status']})", None

    check = _run(tracer, "meterless", "detect", detect)
    if check.status == "pass" and not settings.meterless_base_url:
        settings = replace(settings, meterless_base_url=check.detail.split()[1])
    return check, settings


def check_meterless(settings: Settings, tracer: Tracer,
                    transport: Optional[httpx.BaseTransport] = None) -> list[Check]:
    try:
        client = meterless.meterless_client(settings, transport=transport)
    except AdapterError as e:
        return [Check("meterless", "config", "fail", f"not configured: {e.message}")]

    def models():
        # Status comes from GET /v1/models: online, unauthorized or offline.
        status = meterless.probe(settings.meterless_base_url, settings.meterless_runtime_token,
                                 app_name=settings.meterless_app, timeout_s=settings.http_timeout_s,
                                 transport=transport)
        if status["status"] != "online":
            raise AdapterError("meterless", status["status"], status["detail"],
                               recoverable=status["status"] == "offline")
        ids = status["models"]
        listed = "listed" if settings.meterless_model in ids else "NOT listed"
        nemotron = [i for i in ids if "nemotron" in i.lower()]
        return (f"{len(ids)} model(s); configured {settings.meterless_model!r} {listed}; "
                f"nemotron routes: {_short(nemotron) if nemotron else 'none'}"), None

    def chat():
        result = client.chat([{"role": "user", "content": CHAT_PROMPT}],
                             temperature=0, max_tokens=60)
        tokens = result.usage.get("total_tokens", "?")
        return f"answered by {result.model} ({tokens} tokens): {_short(result.content, 120)!r}", result.model

    try:
        results = [_run(tracer, "meterless", "models", models)]
        if results[0].status != "pass":
            return results + _skip_rest("meterless", ["chat"], "skipped: models failed")
        results.append(_run(tracer, "meterless", "chat", chat))
        return results
    finally:
        client.close()


def _print_report(settings: Settings, results: list[Check], trace_path: Path) -> None:
    headers = {
        "zetaris": f"Zetaris  ({settings.zetaris_rest_url or 'ZETARIS_REST_URL unset'}, "
                   f"org {settings.zetaris_org_id})",
        "meterless": f"Meterless Nightshift  ({settings.meterless_base_url or 'METERLESS_BASE_URL unset'}, "
                     f"model {settings.meterless_model})",
    }
    for service in ("zetaris", "meterless"):
        checks = [c for c in results if c.service == service]
        if not checks:
            continue
        print(headers[service])
        for c in checks:
            elapsed = f"{c.elapsed_s:6.2f}s" if c.status != "skip" else " " * 7
            print(f"  {c.status.upper():4}  {c.name:15} {elapsed}  {c.detail}")
    print(f"\nTrace: {trace_path.relative_to(ROOT).as_posix() if trace_path.is_relative_to(ROOT) else trace_path}")


def main(argv: Optional[list[str]] = None, settings: Optional[Settings] = None,
         transports: Optional[dict[str, httpx.BaseTransport]] = None) -> int:
    parser = argparse.ArgumentParser(description="Check the Zetaris and Meterless Nightshift connections.")
    parser.add_argument("--only", choices=("zetaris", "meterless"))
    parser.add_argument("--retries", type=int, default=0,
                        help="retries per call (default 0, so failures show up fast)")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    parser.add_argument("--detect", action="store_true",
                        help="scan ports 8088/8090/8787/11434 for Nightshift first")
    args = parser.parse_args(argv)

    settings = replace(settings or load_settings(), max_retries=max(0, args.retries))
    transports = transports or {}
    tracer = Tracer(new_run_id(), settings.log_dir, settings.secret_values(), stream=None)

    results: list[Check] = []
    if args.only in (None, "zetaris"):
        results += check_zetaris(settings, tracer, transports.get("zetaris"))
    if args.only in (None, "meterless"):
        if args.detect:
            detected, settings = detect_meterless(settings, tracer, transports.get("meterless"))
            results.append(detected)
        results += check_meterless(settings, tracer, transports.get("meterless"))

    if args.json:
        print(json.dumps({"trace_path": str(tracer.path),
                          "results": [asdict(c) for c in results]}, indent=2))
    else:
        _print_report(settings, results, tracer.path)
    return 0 if all(c.status == "pass" for c in results) else 1


if __name__ == "__main__":
    sys.exit(main())
