"""Entry point: FastAPI on 0.0.0.0:$PORT, the chat UI at /, and the agent APIs.

    GET  /                      the chat UI (floating island; Ctrl+Shift+Space or Ctrl+K opens it)
    POST /chat                  one chat message (+ optional upload_id) -> background job
    GET  /jobs/{id}?after=n     the job's live agent events and, when done, its result
    POST /uploads?filename=     upload a CSV/XLSX (raw body) ; GET /uploads/{id} ; GET /samples
    GET  /tasks/{id}            a spreadsheet task: plan, code, QA, decision ; GET /tasks/{id}/code
    POST /tasks/{id}/decision   human approve/reject (bound to the code hash); approve runs the code
    GET  /files/{file_id}       a filled spreadsheet
    POST /run                   the workforce-analysis pipeline, synchronous (the hackathon's /run contract)
    GET  /approvals[/{id}] ; POST /approvals/{id}
    GET  /traces ; GET /traces/{run_id}   past runs on this machine, for the Team pane's replay
    GET  /health ; GET /config ; GET /providers/meterless
    GET  /data/workforce/{table} ; GET /data/uploads/cells      (what Zetaris pulls as REST tables)

Every run gets a trace file, and every failure comes back as a structured
error ({status: "error", error: {type, message, recoverable}}), never a stack trace.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import data
from app.adapters import meterless
from app.adapters.http import AdapterError
from app.agents.base import AgentError
from app.agents.memory import record_approval
from app.chat import error_view, public_task, start_chat
from app.config import ROOT, Settings, load_settings
from app.llm import build_generator
from app.pipeline import STAGES, run_pipeline
from app.services import Services
from app.tasks import files
from app.tasks.agents import SHEET_PREVIEW_ROWS
from app.tasks.pipeline import execute_task
from app.tracing import Tracer, new_run_id, verify_chain

UI_DIR = ROOT / "app" / "ui"
_RUN_ID = re.compile(r"^[0-9a-f]{16}$")
# Which kind of run a trace is, from the agents that acted in it.
_MODE_AGENTS = (("analysis", {"SourceMapperAgent", "ResearchPlannerAgent"}), ("assist", {"AssistantAgent"}),
                ("task", {"DataProfilerAgent", "OperatorAgent"}))
# What the Team pane replays from each event (the hash-chain fields stay in the file).
_REPLAY_FIELDS = ("timestamp", "run_id", "agent_name", "action", "status", "target_agent", "model", "input_summary",
                  "output_summary", "retry_count", "confidence", "cases", "cases_matched", "cells", "sheet",
                  "rows_out", "cells_changed", "code_hash", "audit_written", "provider", "data_source", "purpose",
                  "executed_sql")
DEMO_PROMPTS = {
    "demo_uae_staff_lookup": "املأ الفريق لكل موظف من دليل الأسماء، أصلح التواريخ، وحوّل المبالغ",
    "demo_expense_claims": ("Fill in each employee's team and role from our staff directory, tidy the category "
                            "names, convert the dates to YYYY-MM-DD, add the amount including 10% GST, flag claims "
                            "over 500 for approval and give me totals by team on a separate sheet."),
    "demo_vendor_invoices": "Remove duplicate invoices, fill blank statuses with Pending, mark unpaid invoices due "
                            "before 2026-10-15 as Overdue and sort by due date.",
    "demo_leads_and_contacts": "Split the full names into first and last name, check each lead against the CRM "
                               "Contacts sheet by email, fill in the owner and mark new leads as 'Create contact'.",
    "demo_uae_site_claims": "Fill in each person's team and role from our staff directory using the Staff ID, "
                            "flag claims over 1,000 AED for approval and give me totals by team on a separate sheet.",
}


class RunRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=200)
    user_query: str = Field(min_length=1, max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    upload_id: Optional[str] = Field(default=None, max_length=60)
    options: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    approver: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=2000)
    code_hash: Optional[str] = Field(default=None, description="if given, must match the requested code")


def _display_path(path: Path) -> str:
    """Repo-relative when possible, so responses don't reveal the host's directory layout."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _error(status_code: int, type: str, message: str, recoverable: bool, *,
           request_id: Optional[str] = None, trace_id: Optional[str] = None,
           log_path: Optional[str] = None, started: Optional[float] = None) -> JSONResponse:
    elapsed = round(time.monotonic() - started, 3) if started is not None else 0.0
    return JSONResponse(status_code=status_code, content={
        "request_id": request_id,
        "trace_id": trace_id,
        "status": "error",
        "error": {"type": type, "message": message, "recoverable": recoverable},
        "log_path": log_path,
        "execution_time_seconds": elapsed,
    })


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="AgentOffice", version="0.2.0")
    services = Services(settings)
    services.uploads.ensure_demos()
    approvals = services.approvals

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                             for e in exc.errors())
        return _error(422, "invalid_request", problems, recoverable=True)

    # ------------------------------------------------------------------ UI

    @app.middleware("http")
    async def _no_stale_ui(request: Request, call_next):
        """The desktop island keeps its own browser profile; never let it run an old cached UI."""
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/ui/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    if UI_DIR.exists():
        app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

        @app.get("/", include_in_schema=False)
        def index():
            # version-stamp the assets, so a browser that cached an older UI fetches the new files
            html = (UI_DIR / "index.html").read_text(encoding="utf-8")
            for name in ("style.css", "app.js"):
                stamp = int((UI_DIR / name).stat().st_mtime)
                html = html.replace(f"/ui/{name}\"", f"/ui/{name}?v={stamp}\"")
            return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ status

    @app.get("/health")
    def health() -> dict:
        # Local only: says how the app is configured, does not call any sponsor service.
        return {
            "status": "ok",
            "sample_mode": settings.sample_mode,
            "llm_provider": settings.llm_provider,
            "missing_config": settings.missing_for_default_mode(),
            # Effective addresses (no secrets): inside a container, localhost is rewritten to the host.
            "endpoints": {"zetaris": settings.zetaris_rest_url or None,
                          "meterless": settings.meterless_base_url or None},
        }

    @app.get("/config")
    def ui_config() -> dict:
        """What the UI shows in its badges. No secrets."""
        ready = not settings.missing_for_default_mode()
        meterless_ready = not any(k.startswith("METERLESS") or k.startswith("MODEL_") or k == "LLM_PROVIDER"
                                  for k in settings.missing_for_default_mode())
        zetaris_ready = "ZETARIS_REST_URL" not in settings.missing_for_default_mode() \
            and "ZETARIS_API_KEY" not in settings.missing_for_default_mode()
        return {"sample_mode": settings.sample_mode or not ready, "default_mode_ready": ready,
                "forced_sample_mode": settings.sample_mode, "model": settings.meterless_model
                if settings.llm_provider == "meterless" else settings.llm_provider,
                "data_source": "Zetaris" if ready and not settings.sample_mode else "sample data",
                "hotkeys": ["Ctrl+Shift+Space", "Ctrl+K"],
                "sponsors": {
                    "meterless": {"ready": meterless_ready, "provider": "Meterless Nightshift",
                                  "model": settings.meterless_model if settings.llm_provider == "meterless"
                                  else settings.llm_provider},
                    "zetaris": {"ready": zetaris_ready, "provider": "Zetaris"},
                }}

    # Past runs on this machine, replayed in the Team pane from their own traces in logs/
    # (the saved example runs committed for judges are never read by the app).
    def _trace_path(run_id: str) -> Optional[Path]:
        path = Path(settings.log_dir) / f"trace-{run_id}.jsonl"
        return path if _RUN_ID.match(run_id) and path.is_file() else None

    def _trace_view(path: Path) -> Optional[dict]:
        try:
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError):
            return None
        agents = {e.get("agent_name") for e in events}
        mode = next((m for m, names in _MODE_AGENTS if agents & names), None)
        if mode is None:         # a plain chat reply: nothing to replay
            return None
        started = next((e for e in events if e.get("action") == "run_started"), events[0])
        when = str(started.get("timestamp", ""))[5:16].replace("T", " ")
        what = " ".join(str(started.get("input_summary") or "").split())
        return {"id": path.stem[len("trace-"):], "mode": mode, "events": events, "at": started.get("timestamp"),
                "label": f"{when} · {mode} · {what[:56] + ('…' if len(what) > 56 else '')}"}

    @app.get("/traces")
    def list_traces() -> dict:
        """The most recent replayable runs on this machine, newest first."""
        paths = sorted(Path(settings.log_dir).glob("trace-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        runs = []
        for path in paths[:40]:
            view = _trace_view(path)
            if view:
                runs.append({k: view[k] for k in ("id", "label", "mode", "at")} | {"count": len(view["events"])})
            if len(runs) == 12:
                break
        return {"runs": runs}

    @app.get("/traces/{run_id}")
    def get_trace(run_id: str):
        path = _trace_path(run_id)
        view = _trace_view(path) if path else None
        if view is None:
            return _error(404, "not_found", f"no replayable run {run_id!r} in {settings.log_dir}", False)
        events = [{k: e.get(k) for k in _REPLAY_FIELDS} | {"i": i} for i, e in enumerate(view["events"])]
        models = sorted({e["model"] for e in events if e.get("model")})
        # Sponsor badges count only calls that happened: sample mode logs its Zetaris calls as "skipped".
        used_zetaris = any(e.get("status") != "skipped" and (
            e.get("target_agent") == "Zetaris" or "zetaris" in str(e.get("data_source") or "").lower())
            for e in events)
        used_meterless = any("meterless" in str(e.get("provider") or "").lower() for e in events)
        return {"id": run_id, "label": view["label"], "mode": view["mode"], "run_id": run_id, "replay": True,
                "chain_intact": verify_chain(path) is None, "events": events, "count": len(events),
                "sponsors": {"models": models, "zetaris": used_zetaris, "meterless": used_meterless}}

    @app.get("/providers/meterless")
    def meterless_status() -> dict:
        """Test Connection for the primary provider. Never returns the token."""
        token = settings.meterless_runtime_token
        base_url = settings.meterless_base_url or meterless.DEFAULT_BASE_URL
        info = {"id": meterless.PROVIDER_ID, "name": meterless.PROVIDER_NAME,
                "primary": settings.llm_provider == "meterless", "base_url": base_url,
                "model": settings.meterless_model, "token_set": bool(token)}
        try:
            meterless.check_token(token)
        except AdapterError as e:
            return {**info, "status": "not_configured", "detail": e.message, "routes": []}
        status = meterless.probe(base_url, token, app_name=settings.meterless_app,
                                 timeout_s=settings.http_timeout_s)
        routes = [m for m in status["models"] if m.startswith("meterless/")]
        return {**info, "status": status["status"], "detail": status["detail"], "routes": routes}

    # ------------------------------------------------------------------ data Zetaris pulls

    tables = {t["table"] for t in data.catalog()}

    @app.get("/data/workforce/{table}")
    def workforce_table(table: str):
        """Serves the synthetic tables for Zetaris to register as REST tables."""
        if table not in tables:
            return _error(404, "not_found", f"no table {table!r}; available: {', '.join(sorted(tables))}", False)
        _, _, rows = data.load_table(table)
        return {"table": table, "synthetic": True, "generator": "scripts/generate_workforce_data.py",
                "rows": rows}

    @app.get("/data/uploads/cells")
    def upload_cells():
        """Recent uploads (and the demo files) as long-format cells: the Zetaris REST table upload_cells."""
        return {"table": data.UPLOAD_CELLS, "rows": services.uploads.all_cells()}

    # ------------------------------------------------------------------ uploads

    @app.post("/uploads")
    async def upload(request: Request, filename: str):
        body = await request.body()
        try:
            meta = services.uploads.save(filename, body)
        except files.FileError as e:
            return _error(422, "bad_file", str(e), True)
        return _upload_view(meta["upload_id"])

    def _upload_view(upload_id: str) -> Optional[dict]:
        meta = services.uploads.meta(upload_id)
        parsed = services.uploads.parsed(upload_id)
        if meta is None or parsed is None:
            return None
        # preview stays short for the chip; rows (capped) feeds the Sheet pane
        return {**meta, "preview": {s["name"]: s["rows"][:6] for s in parsed["sheets"]},
                "columns": {s["name"]: s["columns"] for s in parsed["sheets"]},
                "rows": {s["name"]: s["rows"][:SHEET_PREVIEW_ROWS] for s in parsed["sheets"]},
                "row_totals": {s["name"]: len(s["rows"]) for s in parsed["sheets"]},
                "suggested_prompt": DEMO_PROMPTS.get(upload_id)}

    @app.get("/uploads/{upload_id}")
    def get_upload(upload_id: str):
        view = _upload_view(upload_id)
        return view or _error(404, "not_found", f"no upload {upload_id!r}", False)

    @app.get("/samples")
    def samples() -> dict:
        return {"samples": [{**m, "suggested_prompt": DEMO_PROMPTS.get(m["upload_id"])}
                            for m in services.uploads.ensure_demos()]}

    # ------------------------------------------------------------------ chat + jobs

    @app.post("/chat")
    def chat(req: ChatRequest):
        sample_mode = settings.sample_mode or bool(req.options.get("sample_mode"))  # may switch on, never off
        if req.upload_id:
            if services.uploads.meta(req.upload_id) is None:
                return _error(404, "not_found", f"no upload {req.upload_id!r}", False)
            services.uploads.touch(req.upload_id)    # keep it among the uploads Zetaris serves
        job = start_chat(services, req.message, upload_id=req.upload_id, sample_mode=sample_mode)
        return {"job_id": job.id, "status": job.status, "sample_mode": sample_mode}

    @app.get("/jobs/{job_id}")
    def job_status(job_id: str, after: int = 0):
        view = services.jobs.view(job_id, max(0, after))
        return view or _error(404, "not_found", f"no job {job_id!r}", False)

    # ------------------------------------------------------------------ tasks

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str):
        record = services.tasks.get(task_id)
        return public_task(record, approvals) if record else _error(404, "not_found", f"no task {task_id!r}", False)

    @app.get("/tasks/{task_id}/code")
    def task_code(task_id: str):
        record = services.tasks.get(task_id)
        if record is None:
            return _error(404, "not_found", f"no task {task_id!r}", False)
        return PlainTextResponse(record["code"], media_type="text/x-python", headers={
            "Content-Disposition": f'attachment; filename="{record["spec"]["name"]}.py"'})

    def _run_approved(record: dict, approval: dict) -> dict:
        run_id = new_run_id()
        tracer = Tracer(run_id, settings.log_dir, settings.secret_values())
        sample_mode = bool(record["sample_mode"])
        store, pipeline, builder = services.world(sample_mode)
        tracer.event("Orchestrator", "run_started", input_summary=f"run approved task {record['task_id']}",
                     approval_id=approval["approval_id"])
        return execute_task(record, approval, settings=settings, tracer=tracer, sample_mode=sample_mode,
                            store=store, pipeline=pipeline, builder=builder, approvals=approvals,
                            uploads=services.uploads, tasks=services.tasks)

    @app.post("/tasks/{task_id}/decision")
    def decide_task(task_id: str, body: ApprovalDecision):
        record = services.tasks.get(task_id)
        if record is None or not record.get("approval_id"):
            return _error(404, "not_found", f"no task awaiting approval: {task_id!r}", False)
        decided = _decide(record["approval_id"], body)
        if isinstance(decided, JSONResponse):
            return decided
        record["status"] = "approved" if decided["status"] == "approved" else "rejected"
        services.tasks.save(record)
        if decided["status"] == "approved":
            try:
                record = _run_approved(record, decided)
            except (AgentError, AdapterError) as e:
                view = error_view(e)
                return _error(502, view["type"], view["message"], view["recoverable"])
        return {**public_task(record, approvals), "memory": decided.get("memory")}

    @app.post("/tasks/{task_id}/run")
    def rerun_task(task_id: str):
        record = services.tasks.get(task_id)
        approval = approvals.get(record["approval_id"]) if record and record.get("approval_id") else None
        if approval is None or approval["status"] != "approved":
            return _error(409, "not_approved", "only an approved task can run", False)
        try:
            return public_task(_run_approved(record, approval), approvals)
        except (AgentError, AdapterError) as e:
            view = error_view(e)
            return _error(502, view["type"], view["message"], view["recoverable"])

    @app.get("/files/{file_id}")
    def download(file_id: str):
        path = services.uploads.output_path(file_id)
        if path is None:
            return _error(404, "not_found", f"no file {file_id!r}", False)
        media = ("text/csv" if path.suffix == ".csv"
                 else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        return FileResponse(path, media_type=media, filename=path.name)

    # ------------------------------------------------------------------ approvals

    def _decide(approval_id: str, body: ApprovalDecision):
        try:
            decided = approvals.decide(approval_id, decision=body.decision, approver=body.approver, note=body.note,
                                       code_hash=body.code_hash)
        except KeyError:
            return _error(404, "not_found", f"no approval {approval_id!r}", False)
        except ValueError as e:
            return _error(409, "conflict", str(e), False)
        # The human's decision becomes a long-term H-MEM preference that later runs must respect.
        store, _ = services.memory(bool(decided.get("sample_mode")))
        outcome = record_approval(store, decided)
        return {**decided, "memory": {"status": outcome["status"], "id": outcome["memory"]["id"]
                                      if outcome["memory"] else None, "superseded": [
                                          c["loser"] for c in outcome["conflicts"] if c["decision_confidence"] >= 0.7]}}

    @app.get("/approvals")
    def list_approvals(status: Optional[str] = None) -> dict:
        items = [a for a in approvals.all() if status is None or a["status"] == status]
        return {"approvals": items[::-1][:100]}

    @app.get("/approvals/{approval_id}")
    def get_approval(approval_id: str):
        approval = approvals.get(approval_id)
        if approval is None:
            return _error(404, "not_found", f"no approval {approval_id!r}", False)
        return approval

    @app.post("/approvals/{approval_id}")
    def decide_approval(approval_id: str, body: ApprovalDecision):
        return _decide(approval_id, body)

    # ------------------------------------------------------------------ the analysis pipeline (sync)

    @app.post("/run")
    def run(req: RunRequest):
        started = time.monotonic()
        run_id = new_run_id()
        tracer = Tracer(run_id, settings.log_dir, settings.secret_values())
        log_path = _display_path(tracer.path)
        # A request may switch sample mode on, never off.
        sample_mode = settings.sample_mode or bool(req.options.get("sample_mode"))
        tracer.event("Orchestrator", "run_started", input_summary=req.user_query,
                     request_id=req.request_id, sample_mode=sample_mode)

        def fail(code: int, type: str, message: str, recoverable: bool) -> JSONResponse:
            tracer.event("Orchestrator", "run_failed", status="error",
                         output_summary=message, error_type=type)
            return _error(code, type, message, recoverable, request_id=req.request_id,
                          trace_id=run_id, log_path=log_path, started=started)

        if not sample_mode:
            missing = settings.missing_for_default_mode()
            if missing:
                return fail(503, "not_configured",
                            f"default mode needs {', '.join(missing)} in .env "
                            "(or set SAMPLE_MODE=true)", recoverable=True)

        try:
            generator = None if sample_mode else build_generator(settings)
            store, pipeline, builder = services.world(sample_mode)
            mem_store, recall = services.memory(sample_mode)
            stages = req.options.get("stages", list(STAGES))
            if not isinstance(stages, list) or not stages or not set(stages) <= set(STAGES):
                return fail(422, "invalid_request", f"options.stages must be a list drawn from {list(STAGES)}", True)
            result = run_pipeline(req.user_query, req.context, settings=settings, tracer=tracer,
                                  generator=generator, sample_mode=sample_mode, store=store, pipeline=pipeline,
                                  builder=builder, approvals=approvals, memory=mem_store, recall=recall,
                                  stages=stages, request_id=req.request_id)
        except AdapterError as e:
            err = e.to_dict()
            return fail(503, err["type"], err["message"], e.recoverable)
        except AgentError as e:
            return fail(502, f"agent_{e.type}", tracer.mask(str(e)), e.recoverable)
        except Exception as e:  # last resort: report, don't dump a stack trace
            return fail(500, "internal_error", f"{type(e).__name__}: {tracer.mask(str(e))}", False)

        tracer.event("Orchestrator", "run_completed", output_summary=result["result"]["summary"],
                     confidence=result["result"]["confidence"])
        return {
            "request_id": req.request_id,
            "trace_id": run_id,
            "status": "success",
            **result,
            "log_path": log_path,
            "execution_time_seconds": round(time.monotonic() - started, 3),
        }

    return app


settings = load_settings()
app = create_app(settings)

if __name__ == "__main__":
    # 0.0.0.0, not 127.0.0.1, so the port is reachable from outside a Docker container.
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level=settings.log_level.lower())
