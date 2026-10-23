"""Run every input example through POST /run and save the real responses.

    python scripts/run_examples.py --sample          # sample mode -> output_examples/sample_mode/
    python scripts/run_examples.py                   # default mode (Zetaris + Nightshift) -> output_examples/
    python scripts/run_examples.py --url http://localhost:8000   # against a running server instead of in-process

Outputs are saved exactly as the API returned them, with the trace file next
to each. They are evidence for humans; the application never reads them.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run input_examples/*.json and save real outputs.")
    parser.add_argument("--sample", action="store_true", help="force sample mode for every example")
    parser.add_argument("--url", help="POST to this running server instead of running in-process")
    args = parser.parse_args(argv)

    out_dir = ROOT / "output_examples" / ("sample_mode" if args.sample else "")
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.url:
        import httpx
        post = lambda body: httpx.post(args.url.rstrip("/") + "/run", json=body, timeout=900).json()
    else:
        from fastapi.testclient import TestClient
        import run
        client = TestClient(run.app)
        post = lambda body: client.post("/run", json=body).json()

    failures = 0
    for path in sorted((ROOT / "input_examples").glob("example_*.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        if args.sample:
            body["options"] = {**body.get("options", {}), "sample_mode": True}
        result = post(body)
        (out_dir / path.name).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        trace = ROOT / str(result.get("log_path") or "")
        if result.get("log_path") and trace.is_file():
            shutil.copyfile(trace, out_dir / path.name.replace(".json", ".trace.jsonl"))
        ok = result.get("status") == "success"
        failures += not ok
        summary = result["result"]["summary"] if ok else result.get("error")
        print(f"{'OK  ' if ok else 'FAIL'} {path.name}: {summary}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
