"""One chat message -> one background job: the front desk routes it, then the right team runs.

    task      (a spreadsheet is attached)  app/tasks/pipeline.py: plan, build, test, ask for approval
    analysis  (where does the time go?)     app/pipeline.py: discover, explain, build (the original pipeline)
    question  (who / how many / which ...)  app/assistant.py: look it up in the data, answer, check every number
    email     (draft an email to ...)       app/assistant.py: the same, as an email draft the user copies
    reply     (anything else)               the front desk answers

Every job has its own trace file; the UI follows it live through the job's events.
"""

from __future__ import annotations

from typing import Optional

from app.adapters.http import AdapterError
from app.agents.base import AgentError, RunContext
from app.approvals import ApprovalLedger
from app.assistant import assist
from app.jobs import Job
from app.llm import build_generator
from app.pipeline import run_pipeline
from app.services import Services
from app.tasks.agents import FrontDesk
from app.tasks.pipeline import execute_task, run_task
from app.tracing import Tracer, new_run_id


def public_task(record: dict, approvals: Optional[ApprovalLedger] = None) -> dict:
    """A task record for the UI: reference data and the profile summarised, approval state attached."""
    out = {k: v for k, v in record.items() if k not in ("reference", "profile")}
    out["reference"] = {name: {"rows": len(rows), "sample": rows[:3]} for name, rows in record["reference"].items()}
    sheets = record.get("profile", {}).get("sheets", [])
    out["profile"] = [{"sheet": s["name"], "rows": s["rows"], "columns": len(s["columns"])} for s in sheets]
    out["input_columns"] = next((
        [c["name"] for c in s["columns"]] for s in sheets if s["name"] == record["spec"]["input"]["sheet"]), [])
    if approvals and record.get("approval_id"):
        out["approval"] = approvals.get(record["approval_id"])
    return out


def error_view(e: Exception, mask=lambda s: s) -> dict:
    if isinstance(e, AdapterError):
        err = e.to_dict()
        return {"type": err["type"], "message": mask(err["message"]), "recoverable": e.recoverable}
    if isinstance(e, AgentError):
        return {"type": f"agent_{e.type}", "message": mask(str(e)), "recoverable": e.recoverable}
    return {"type": "internal_error", "message": f"{type(e).__name__}: {mask(str(e))}", "recoverable": False}


def start_chat(services: Services, message: str, *, upload_id: Optional[str], sample_mode: bool) -> Job:
    settings = services.settings
    holder: dict = {}

    def run(job: Job) -> dict:
        run_id = new_run_id()
        job.trace_id = run_id
        tracer = Tracer(run_id, settings.log_dir, settings.secret_values(), listener=job.on_event)
        holder["tracer"] = tracer
        tracer.event("Orchestrator", "run_started", input_summary=message, sample_mode=sample_mode,
                     upload_id=upload_id)
        if not sample_mode and settings.missing_for_default_mode():
            raise AgentError("Orchestrator", "not_configured", "default mode needs "
                             f"{', '.join(settings.missing_for_default_mode())} in .env (or use sample mode)", True)
        generator = None if sample_mode else build_generator(settings)
        store, pipeline, builder = services.world(sample_mode)
        mem_store, recall = services.memory(sample_mode)
        desk = RunContext(run_id=run_id, settings=settings, tracer=tracer, generator=generator, data=None,
                          store=store, pipeline=pipeline, builder=builder, sample_mode=sample_mode)
        routed = FrontDesk(desk).route(message, has_file=bool(upload_id))
        job.kind = routed["intent"]
        if routed["intent"] == "task":
            if not upload_id:
                return {"type": "reply", "text": routed["reply"] + " Attach a CSV or Excel file and I'll start."}
            record = run_task(message, upload_id, settings=settings, tracer=tracer, generator=generator,
                              sample_mode=sample_mode, store=store, pipeline=pipeline, builder=builder,
                              approvals=services.approvals, uploads=services.uploads, tasks=services.tasks,
                              memory=mem_store, recall=recall)
            if record["status"] == "approved":   # this exact code was approved before (H-MEM): run it now
                record = execute_task(record, services.approvals.get(record["approval_id"]), settings=settings,
                                      tracer=tracer, sample_mode=sample_mode, store=store, pipeline=pipeline,
                                      builder=builder, approvals=services.approvals, uploads=services.uploads,
                                      tasks=services.tasks)
            tracer.event("Orchestrator", "run_completed", output_summary=f"task {record['task_id']}: {record['status']}")
            return {"type": "task", "reply": routed["reply"], "task": public_task(record, services.approvals)}
        if routed["intent"] == "analysis":
            result = run_pipeline(message, {}, settings=settings, tracer=tracer, generator=generator,
                                  sample_mode=sample_mode, store=store, pipeline=pipeline, builder=builder,
                                  approvals=services.approvals, memory=mem_store, recall=recall,
                                  request_id=job.id)
            tracer.event("Orchestrator", "run_completed", output_summary=result["result"]["summary"],
                         confidence=result["result"]["confidence"])
            return {"type": "analysis", "reply": routed["reply"], "analysis": result}
        if routed["intent"] in ("question", "email"):
            result = assist(routed["intent"], message, upload_id, settings=settings, tracer=tracer,
                            generator=generator, sample_mode=sample_mode, store=store, pipeline=pipeline,
                            builder=builder, uploads=services.uploads)
            tracer.event("Orchestrator", "run_completed", output_summary=f"{routed['intent']} drafted")
            return {"type": "assist", "reply": routed["reply"], "assist": result}
        tracer.event("Orchestrator", "run_completed", output_summary="replied")
        return {"type": "reply", "text": routed["reply"]}

    def on_error(e: Exception) -> dict:
        tracer = holder.get("tracer")
        view = error_view(e, tracer.mask if tracer else (lambda s: s))
        if tracer:
            tracer.event("Orchestrator", "run_failed", status="error", output_summary=view["message"],
                         error_type=view["type"])
        return view

    return services.jobs.start("chat", message, run, on_error)
