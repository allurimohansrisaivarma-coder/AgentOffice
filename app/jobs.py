"""Background jobs for the chat UI: a run in a thread, its trace events streamed as they happen.

The UI polls GET /jobs/<id>?after=<n> and shows each agent step live; the
full, hash-chained trace still goes to logs/trace-<run_id>.jsonl.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Optional

MAX_JOBS = 200
MAX_EVENTS = 400


class Job:
    def __init__(self, kind: str, message: str):
        self.id = "job_" + uuid.uuid4().hex[:12]
        self.kind = kind
        self.message = message
        self.status = "running"          # running | completed | failed
        self.created = time.time()
        self.events: list[dict] = []
        self.result: Optional[dict] = None
        self.error: Optional[dict] = None
        self.trace_id: Optional[str] = None
        self._lock = threading.Lock()

    def on_event(self, event: dict) -> None:
        """Tracer listener: keep a short, already-masked view of each event."""
        with self._lock:
            if len(self.events) >= MAX_EVENTS:
                return
            self.events.append({"i": len(self.events), "at": event["timestamp"], "agent": event["agent_name"],
                                "action": event["action"], "status": event["status"],
                                "target": event.get("target_agent"), "model": event.get("model"),
                                "summary": event["output_summary"][:240]})

    def view(self, after: int = 0) -> dict:
        with self._lock:
            return {"job_id": self.id, "kind": self.kind, "status": self.status, "trace_id": self.trace_id,
                    "events": self.events[after:], "next": len(self.events), "result": self.result,
                    "error": self.error, "elapsed_s": round(time.time() - self.created, 1)}


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, message: str, fn: Callable[[Job], dict],
              on_error: Callable[[Exception], dict]) -> Job:
        job = Job(kind, message)
        with self._lock:
            finished = sorted((j for j in self._jobs.values() if j.status != "running"), key=lambda j: j.created)
            for old in finished[:max(0, len(self._jobs) - MAX_JOBS + 1)]:
                self._jobs.pop(old.id, None)
            self._jobs[job.id] = job

        def target() -> None:
            try:
                result = fn(job)
                with job._lock:
                    job.result, job.status = result, "completed"
            except Exception as e:  # reported to the UI as a structured error, never a stack trace
                error = on_error(e)
                with job._lock:
                    job.error, job.status = error, "failed"

        threading.Thread(target=target, name=job.id, daemon=True).start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def view(self, job_id: str, after: int = 0) -> Optional[dict[str, Any]]:
        job = self.get(job_id)
        return job.view(after) if job else None
