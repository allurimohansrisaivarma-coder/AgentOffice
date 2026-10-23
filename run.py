"""Entry point: FastAPI on 0.0.0.0:$PORT with POST /run and GET /health.

Every /run gets a trace file, and every failure comes back as a structured
error (AGENTS.md section 9), never a stack trace.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import data
from app.adapters import meterless
from app.adapters.http import AdapterError
from app.agents.base import AgentError
from app.agents.memory import record_approval
from app.approvals import ApprovalLedger
from app.config import ROOT, Settings, load_settings
from app.engines.hmem import open_memory
from app.engines.world_model import open_world
from app.llm import build_generator
from app.pipeline import STAGES, run_pipeline
from app.tracing import Tracer, new_run_id


class RunRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=200)
    user_query: str = Field(min_length=1, max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict)
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
    app = FastAPI(title="AgentOffice", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                             for e in exc.errors())
        return _error(422, "invalid_request", problems, recoverable=True)

    @app.get("/health")
    def health() -> dict:
        # Local only: says how the app is configured, does not call any sponsor service.
        return {
            "status": "ok",
            "sample_mode": settings.sample_mode,
            "llm_provider": settings.llm_provider,
            "missing_config": settings.missing_for_default_mode(),
        }

    tables = {t["table"] for t in data.catalog()}

    @app.get("/data/workforce/{table}")
    def workforce_table(table: str):
        """Serves the synthetic tables for Zetaris to register as REST tables."""
        if table not in tables:
            return _error(404, "not_found", f"no table {table!r}; available: {', '.join(sorted(tables))}", False)
        _, _, rows = data.load_table(table)
        return {"table": table, "synthetic": True, "generator": "scripts/generate_workforce_data.py",
                "rows": rows}

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

    approvals = ApprovalLedger(settings.world_db_path.parent)
    worlds: dict[bool, tuple] = {}
    memories: dict[bool, tuple] = {}

    def memory(sample_mode: bool) -> tuple:
        """One H-MEM store per mode, kept apart like the World Models."""
        if sample_mode not in memories:
            memories[sample_mode] = open_memory(settings, sample_mode)
        return memories[sample_mode]

    def world(sample_mode: bool) -> tuple:
        """One shared World Model per mode, so sample runs never mix with real Zetaris facts."""
        if sample_mode not in worlds:
            path = settings.world_db_path
            worlds[sample_mode] = open_world(replace(settings, world_db_path=path.with_name(
                path.stem + "-sample" + path.suffix)) if sample_mode else settings)
        return worlds[sample_mode]

    @app.get("/approvals/{approval_id}")
    def get_approval(approval_id: str):
        approval = approvals.get(approval_id)
        if approval is None:
            return _error(404, "not_found", f"no approval {approval_id!r}", False)
        return approval

    @app.post("/approvals/{approval_id}")
    def decide_approval(approval_id: str, body: ApprovalDecision):
        try:
            decided = approvals.decide(approval_id, decision=body.decision, approver=body.approver, note=body.note,
                                       code_hash=body.code_hash)
        except KeyError:
            return _error(404, "not_found", f"no approval {approval_id!r}", False)
        except ValueError as e:
            return _error(409, "conflict", str(e), False)
        # The human's decision becomes a long-term H-MEM preference that later runs must respect.
        store, _ = memory(bool(decided.get("sample_mode")))
        outcome = record_approval(store, decided)
        return {**decided, "memory": {"status": outcome["status"], "id": outcome["memory"]["id"]
                                      if outcome["memory"] else None, "superseded": [
                                          c["loser"] for c in outcome["conflicts"] if c["decision_confidence"] >= 0.7]}}

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
            store, pipeline, builder = world(sample_mode)
            mem_store, recall = memory(sample_mode)
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
