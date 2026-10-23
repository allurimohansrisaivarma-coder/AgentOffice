"""Entry point: FastAPI on 0.0.0.0:$PORT with POST /run and GET /health.

Every /run gets a trace file, and every failure comes back as a structured
error (AGENTS.md section 9), never a stack trace.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.adapters.http import AdapterError
from app.config import ROOT, Settings, load_settings
from app.llm import build_generator
from app.pipeline import run_pipeline
from app.tracing import Tracer, new_run_id


class RunRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=200)
    user_query: str = Field(min_length=1, max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


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
            result = run_pipeline(req.user_query, req.context, settings=settings,
                                  tracer=tracer, generator=generator)
        except AdapterError as e:
            err = e.to_dict()
            return fail(503, err["type"], err["message"], e.recoverable)
        except NotImplementedError as e:
            return fail(501, "not_implemented", str(e), False)
        except Exception as e:  # last resort: report, don't dump a stack trace
            return fail(500, "internal_error", f"{type(e).__name__}: {tracer.mask(str(e))}", False)

        tracer.event("Orchestrator", "run_completed", output_summary=result.get("result", {}))
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
