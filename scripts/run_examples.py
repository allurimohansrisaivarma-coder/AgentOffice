"""Run every input example through POST /run and save the real responses.

    python scripts/run_examples.py --sample          # sample mode -> output_examples/sample_mode/
    python scripts/run_examples.py                   # default mode (Zetaris + Nightshift) -> output_examples/
    python scripts/run_examples.py --url http://localhost:8000   # against a running server instead of in-process
    python scripts/run_examples.py --tasks [--sample] [--approver "name"]   # the spreadsheet demo tasks via /chat

Outputs are saved exactly as the API returned them, with the trace file next
to each. They are evidence for humans; the application never reads them.
With --tasks, each demo file goes through POST /chat; with --approver the
pending approval is decided as that (named, scripted) approver and the filled
workbook is saved too; without it the task stops at awaiting_approval.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _client(url):
    """(get, post, get_bytes) against a running server or the app in-process."""
    if url:
        import httpx
        base = url.rstrip("/")
        return (lambda p: httpx.get(base + p, timeout=60).json(),
                lambda p, body: httpx.post(base + p, json=body, timeout=900).json(),
                lambda p: httpx.get(base + p, timeout=60).content)
    from fastapi.testclient import TestClient
    import run
    client = TestClient(run.app)
    return (lambda p: client.get(p).json(), lambda p, body: client.post(p, json=body).json(),
            lambda p: client.get(p).content)


def run_tasks(out_dir: Path, get, post, get_bytes, *, sample: bool, approver: str | None) -> int:
    failures = 0
    for demo in get("/samples")["samples"]:
        name = demo["upload_id"].removeprefix("demo_")
        job = post("/chat", {"message": demo["suggested_prompt"], "upload_id": demo["upload_id"],
                             "options": {"sample_mode": sample}})
        view = get(f"/jobs/{job['job_id']}")
        while view["status"] == "running":
            time.sleep(2)
            view = get(f"/jobs/{job['job_id']}")
        task = (view.get("result") or {}).get("task")
        if task and task["status"] == "awaiting_approval" and approver:
            task = post(f"/tasks/{task['task_id']}/decision", {"decision": "approve", "approver": approver,
                                                               "note": "scripted demo approval (run_examples.py)",
                                                               "code_hash": task["code_hash"]})
            view["approval_decision"] = {k: task.get(k) for k in ("status", "approval", "outputs", "memory")}
        if task and task.get("outputs"):
            for f in task["outputs"]["files"]:
                (out_dir / f"task_{name}.filled.{f['format']}").write_bytes(get_bytes(f"/files/{f['file_id']}"))
        (out_dir / f"task_{name}.json").write_text(json.dumps(view, indent=2, ensure_ascii=False) + "\n",
                                                   encoding="utf-8")
        trace = ROOT / "logs" / f"trace-{view.get('trace_id')}.jsonl"
        if trace.is_file():
            shutil.copyfile(trace, out_dir / f"task_{name}.trace.jsonl")
        ok = view["status"] == "completed" and task is not None and task.get("qa", {}).get("passed")
        failures += not ok
        print(f"{'OK  ' if ok else 'FAIL'} task_{name}: " + (f"{task['status']}, QA {task['qa']['pass_rate']:.0%} of "
              f"{task['qa']['cases']} cases, {task['code_origin']}" if task else str(view.get("error"))))
    return failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run input_examples/*.json and save real outputs.")
    parser.add_argument("--sample", action="store_true", help="force sample mode for every example")
    parser.add_argument("--url", help="POST to this running server instead of running in-process")
    parser.add_argument("--tasks", action="store_true", help="run the spreadsheet demo tasks instead")
    parser.add_argument("--approver", help="with --tasks: approve each passing task as this named approver")
    args = parser.parse_args(argv)

    out_dir = ROOT / "output_examples" / ("sample_mode" if args.sample else "")
    out_dir.mkdir(parents=True, exist_ok=True)
    get, post_path, get_bytes = _client(args.url)
    if args.tasks:
        return 1 if run_tasks(out_dir, get, post_path, get_bytes, sample=args.sample, approver=args.approver) else 0
    post = lambda body: post_path("/run", body)

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
