"""Task mode: "do this to my spreadsheet" -> plan -> build -> test -> approve -> run.

    Front desk routes the message (app/chat.py) ->
    DataProfiler (Zetaris SQL over upload_cells) -> Memory Keeper (approved automations + lessons from H-MEM)
    -> PM <-> Spec Critic (+ Retriever for reference data, capped) -> Engineer -> QA -> Engineer (revise, capped)
    -> Manager (rule gate) -> human approval bound to the code hash -> Operator runs exactly that code.

A task already approved for the same code (H-MEM remembers the decision) runs
without asking again; everything else waits for a human.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Optional

from app.adapters.http import AdapterError
from app.agents.base import Agent, AgentError, RunContext
from app.agents.memory import MemoryKeeper
from app.approvals import ApprovalLedger
from app.config import Settings
from app.data import build_data_source
from app.engines.hmem import MemoryRetrievalService, MemoryStoreService
from app.engines.world_model import (
    ContextBuilderService, IngestPipelineService, WorldStoreService, entity_observation, relationship_observation,
)
from app.engines.world_model.ids import utc_now
from app.llm import Generator
from app.tasks import files
from app.tasks.agents import (
    DataProfiler, Operator, Retriever, TaskEngineer, TaskManager, TaskPlanner, TaskQA, critique,
)
from app.tracing import Tracer

MAX_SPEC_ROUNDS = 2


class TaskStore:
    """One JSON record per task under state/tasks/: what was asked, planned, built, tested and decided."""

    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir) / "tasks"
        self._lock = threading.Lock()

    def save(self, record: dict) -> dict:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / f"{record['task_id']}.json").write_text(json.dumps(record, ensure_ascii=False, default=str),
                                                               encoding="utf-8")
        return record

    def get(self, task_id: str) -> Optional[dict]:
        path = self.dir / f"{Path(task_id).name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class TaskOrchestrator(Agent):
    name = "Orchestrator"


def approved_automations(approvals: ApprovalLedger, tasks: TaskStore, memory: Optional[MemoryStoreService]) -> dict:
    """Task automations a human approved, as H-MEM remembers them: {name: {"spec", "code_hash", ...}}."""
    if memory is None:
        return {}
    ledger = {a["code_hash"]: a for a in approvals.all() if a["kind"] == "task" and a["status"] == "approved"}
    out = {}
    for m in memory.find(fact_kind="approval_decision"):
        facts = m["facts"]
        approval = ledger.get(facts.get("code_hash"))
        if facts.get("decision") != "approved" or approval is None:
            continue
        record = tasks.get(approval["task_id"]) if approval.get("task_id") else None
        if record:
            out[record["spec"]["name"]] = {"spec": record["spec"], "code_hash": approval["code_hash"],
                                           "approval_id": approval["approval_id"], "memory": m["content"],
                                           "memory_id": m["id"]}
    return out


def _fetch_references(retriever: Retriever, spec: dict, parsed: dict) -> tuple[dict, list[str]]:
    reference, issues = {}, []
    for name, ref in (spec.get("references") or {}).items():
        try:
            reference[name] = retriever.fetch(name, ref, parsed)
        except AgentError as e:
            issues.append(e.message)
        except AdapterError as e:
            issues.append(f"reference {name!r}: the SQL failed on {retriever.ctx.data.name}: {e.message[:300]}")
    return reference, issues


def run_task(task: str, upload_id: str, *, settings: Settings, tracer: Tracer, generator: Optional[Generator],
             sample_mode: bool, store: WorldStoreService, pipeline: IngestPipelineService,
             builder: ContextBuilderService, approvals: ApprovalLedger, uploads: files.UploadStore,
             tasks: TaskStore, memory: Optional[MemoryStoreService] = None,
             recall: Optional[MemoryRetrievalService] = None, max_revisions: int = 3) -> dict:
    meta, parsed = uploads.meta(upload_id), uploads.parsed(upload_id)
    if meta is None or parsed is None:
        raise AgentError("Orchestrator", "not_found", f"no upload {upload_id!r}; upload the file first")
    data = build_data_source(settings, sample_mode,
                             upload_cells=uploads.all_cells(include=upload_id) if sample_mode else None)
    ctx = RunContext(run_id=tracer.run_id, settings=settings, tracer=tracer, generator=generator, data=data,
                     store=store, pipeline=pipeline, builder=builder, sample_mode=sample_mode, memory=memory,
                     recall=recall, max_revisions=max_revisions)
    orchestrator = TaskOrchestrator(ctx)
    ctx.agents_used.remove("Orchestrator")
    task_id = "task_" + uuid.uuid4().hex[:12]
    sizes = ", ".join(f"{s['name']}: {s['rows']} rows" for s in meta["sheets"])
    orchestrator.log("plan_run", input_summary=task, output_summary=f"task mode on {meta['filename']} ({sizes}); "
                     f"model: {ctx.model_name}; data: {data.name}", task_id=task_id, upload_id=upload_id)
    sheets = files.sheet_columns(parsed)
    try:
        profile = DataProfiler(ctx).profile(upload_id, parsed)

        keeper = MemoryKeeper(ctx) if memory is not None and recall is not None else None
        approved = approved_automations(approvals, tasks, memory)
        lessons = []
        if keeper:
            recalled = recall.retrieve(task, tags=["lesson"], actor=keeper.name, top_n=3)
            lessons = [m["content"] for m in recalled["memories"]]
            keeper.log("hmem_retrieve", target_agent="PMAgent", input_summary=task, output_summary={
                "approved_automations": [f"{n}: {a['memory'][:100]}" for n, a in approved.items()] or "none",
                "lessons": [l[:100] for l in lessons] or "none"})

        pm, retriever = TaskPlanner(ctx), Retriever(ctx)
        plan = pm.plan(task, profile, sheets, approved, lessons)
        critiques: list[list[str]] = []
        rounds = 0
        if "SpecCritic" not in ctx.agents_used:
            ctx.agents_used.append("SpecCritic")
        while True:
            spec = plan["spec"]
            reference, issues = _fetch_references(retriever, spec, parsed)
            if not issues:
                more, advisories = critique(spec, parsed, reference)
                issues += more
            else:
                advisories = []
            critiques.append(issues)
            tracer.event("SpecCritic", "critique_spec", target_agent="PMAgent",
                         status="needs_revision" if issues else "approved", retry_count=rounds,
                         input_summary={"spec": spec["name"], "steps": [s["op"] for s in spec["steps"]]},
                         output_summary=issues or (advisories or "the spec fits the file and the reference data"))
            if not issues:
                break
            if rounds >= MAX_SPEC_ROUNDS or ctx.generator is None or plan.get("reuse"):
                raise AgentError("SpecCritic", "spec_rejected", "the plan does not fit the file: "
                                 + "; ".join(issues[:3]), recoverable=True)
            rounds += 1
            plan = pm.plan(task, profile, sheets, approved, lessons, revise_with=(spec, issues))

        rows = files.sheet(parsed, spec["input"]["sheet"])["rows"]
        engineer, qa_agent, manager = TaskEngineer(ctx), TaskQA(ctx), TaskManager(ctx)
        if plan.get("reuse"):
            prior_ok = approved[plan["reuse"]]
            code = approvals.code(prior_ok["code_hash"])
            if code is None:
                raise AgentError("Orchestrator", "missing_code", f"approved code {prior_ok['code_hash'][:12]} is gone")
            draft = engineer._draft(code, 1, f"reused: approved in {prior_ok['approval_id']} (H-MEM)")
            orchestrator.log("handoff", target_agent="QAAgent", output_summary=
                             f"reusing approved automation {plan['reuse']} ({prior_ok['code_hash'][:12]}); QA re-tests "
                             "it on this file")
        else:
            orchestrator.log("handoff", target_agent="EngineerAgent", output_summary=f"spec {spec['name']}: "
                             f"{len(spec['steps'])} steps, references {sorted(reference) or 'none'}")
            draft = engineer.build(spec, lessons)
        history = []
        for round_no in range(1, max_revisions + 2):
            report = qa_agent.review(spec, draft, rows, reference, round_no=round_no)
            history.append({"round": round_no, "code_version": draft["version"], "code_hash": draft["code_hash"],
                            "passed": report["passed"], "pass_rate": report["pass_rate"],
                            "failed_checks": [c["name"] for c in report["checks"] if not c["passed"]],
                            "failure_purposes": sorted({f["purpose"] for f in report["failures"]})[:5]})
            if report["passed"] or round_no > max_revisions or plan.get("reuse"):
                break
            draft = engineer.revise(spec, draft, report)

        prior = next((a for a in reversed(approvals.all()) if a["kind"] == "task"
                      and a["code_hash"] == draft["code_hash"] and a["status"] != "pending"), None)
        memo = manager.decide(task, plan, draft, report, advisories, prior)
        approval = None
        if memo["decision"] == "approve_for_human_review":
            approval = approvals.request(run_id=ctx.run_id, automation=spec["name"], routine=spec["name"],
                                         code=draft["code"], report=report, decision_memo=memo,
                                         sample_mode=sample_mode, kind="task", task_id=task_id)
            manager.log("request_approval", target_agent="Human", status="pending",
                        output_summary=f"{approval['approval_id']} for code {approval['code_hash'][:12]}")
        elif memo["decision"] == "already_approved":
            approval = prior
            manager.log("approval_on_record", target_agent="OperatorAgent", status="applied",
                        output_summary=f"{prior['approver']} approved this exact code in {prior['approval_id']}; "
                                       "no new approval needed")

        status = {"approve_for_human_review": "awaiting_approval", "already_approved": "approved",
                  "escalate_to_human": "escalated", "reject": "rejected"}[memo["decision"]]
        record = {"task_id": task_id, "task": task, "upload_id": upload_id, "filename": meta["filename"],
                  "format": meta["format"], "created_at": utc_now(), "run_id": ctx.run_id, "status": status,
                  "sample_mode": sample_mode, "model": ctx.model_name, "data_source": data.name,
                  "understanding": plan.get("understanding"), "assumptions": plan.get("assumptions", []),
                  "reused": plan.get("reuse"), "spec": spec, "spec_rounds": rounds, "critiques": critiques,
                  "advisories": advisories, "reference": reference, "code": draft["code"],
                  "code_hash": draft["code_hash"], "code_version": draft["version"], "code_origin": draft["origin"],
                  "qa": {k: report[k] for k in ("passed", "pass_rate", "cases", "model_cases", "checks", "advisories",
                                                "output_rows", "preview")},
                  "qa_failures": report["failures"][:8], "qa_rounds": history, "memo": memo,
                  "approval_id": approval["approval_id"] if approval else None, "outputs": None,
                  "profile": profile}
        tasks.save(record)
        run_ctx = [("run", ctx.run_id)]
        manager.remember([
            entity_observation("spreadsheet", meta["filename"], source=manager.source(), contexts=run_ctx,
                               attributes={"upload_id": upload_id, "sheets": meta["sheets"]}),
            entity_observation("automation", spec["name"], source=manager.source(), contexts=run_ctx,
                               description=spec["summary"],
                               attributes={"kind": "spreadsheet_task", "code_hash": draft["code_hash"],
                                           "qa_passed": report["passed"], "qa_pass_rate": report["pass_rate"],
                                           "decision": memo["decision"], "task_id": task_id}),
            relationship_observation(("automation", spec["name"]), "processes", ("spreadsheet", meta["filename"]),
                                     source=manager.source(), contexts=run_ctx),
        ], "task automation decision")
        if keeper:
            keeper.mine({"run_id": ctx.run_id, "request_id": task_id, "routines": [spec["name"]],
                         "routine": spec["name"], "automation": spec["name"], "qa_rounds": history,
                         "qa_failure_purposes": {h["round"]: h["failure_purposes"] for h in history},
                         "priority_issues": [], "conflict_assessments": [], "insight_objections": [],
                         "decision": memo["decision"], "confidence": memo["confidence"]})
    finally:
        close = getattr(data, "close", None)
        if close:
            close()
    record["agents_used"] = list(ctx.agents_used)
    record["usage"] = {"model_calls": ctx.model_calls, "data_queries": ctx.queries}
    return tasks.save(record)


def execute_task(record: dict, approval: dict, *, settings: Settings, tracer: Tracer, sample_mode: bool,
                 store: WorldStoreService, pipeline: IngestPipelineService, builder: ContextBuilderService,
                 approvals: ApprovalLedger, uploads: files.UploadStore, tasks: TaskStore) -> dict:
    """Run the approved code on the uploaded file and write the output workbook."""
    parsed = uploads.parsed(record["upload_id"])
    if parsed is None:
        raise AgentError("OperatorAgent", "not_found", f"upload {record['upload_id']} is gone")
    code = approvals.code(approval["code_hash"])
    if code is None:
        raise AgentError("OperatorAgent", "missing_code", "the approved code is not on disk")
    ctx = RunContext(run_id=tracer.run_id, settings=settings, tracer=tracer, generator=None, data=None,
                     store=store, pipeline=pipeline, builder=builder, sample_mode=sample_mode)
    outputs = Operator(ctx).execute(record, approval, code, parsed, uploads)
    record.update({"status": "completed", "outputs": outputs, "executed_at": utc_now(),
                   "executed_with": approval["approval_id"], "execution_run_id": tracer.run_id})
    return tasks.save(record)
