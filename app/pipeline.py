"""The AgentOffice run: one pipeline, three stages, one shared World Model.

    1 Discover  Source Mapper -> Reconciler -> World Model -> Conflict Critic          (Track 1)
    2 Explain   Research Planner -> Retriever <-> data -> Report Writer <-> Insight Critic   (Track 2)
    3 Build     Memory Keeper (recall) -> PM <-> Priority Critic -> Engineer -> QA -> Engineer (revise,
                capped) -> QA -> Manager -> human approval request bound to the code hash   (Track 3, primary)
    end         Memory Keeper mines the run into H-MEM

Every step is traced; everything learned is written to the World Model, and
each stage reads what the earlier ones wrote. H-MEM carries lessons and human
decisions from one run to the next.
"""

from __future__ import annotations

from typing import Iterable, Optional

from app.agents.base import Agent, RunContext
from app.agents.memory import MemoryKeeper
from app.agents.stage1 import ConflictCritic, Reconciler, SourceMapper, team_from_query
from app.agents.stage2 import explain
from app.agents.stage3 import Engineer, Manager, ProductManager, QAAgent
from app.analysis.routines import gather
from app.approvals import ApprovalLedger
from app.config import Settings
from app.data import build_data_source, catalog
from app.engines.hmem import MemoryRetrievalService, MemoryStoreService
from app.engines.world_model import (
    ContextBuilderService, IngestPipelineService, WorldStoreService, entity_observation, relationship_observation,
)
from app.llm import Generator
from app.tracing import Tracer

STAGES = ("discover", "explain", "build")


class Orchestrator(Agent):
    name = "Orchestrator"


def _discover(ctx: RunContext, query: str, tables: list[dict]) -> tuple[dict, dict, dict]:
    mapping = SourceMapper(ctx).map(query, tables)
    reconciler = Reconciler(ctx)
    evidence = gather(lambda sql, purpose: reconciler.query(sql, purpose))
    recon = reconciler.reconcile(evidence)
    conflicts = ConflictCritic(ctx).review()
    return evidence, conflicts, {"sources": mapping, "reconciliation": recon, "conflicts": conflicts}


def _build(ctx: RunContext, orchestrator: Orchestrator, keeper: Optional[MemoryKeeper], query: str, evidence: dict,
           explained: Optional[dict], conflicts: dict, approvals: ApprovalLedger, max_revisions: int) -> dict:
    pm, engineer, qa, manager = ProductManager(ctx), Engineer(ctx), QAAgent(ctx), Manager(ctx)
    world = ctx.builder.build(query, types=["routine", "automation_candidate", "automation", "task",
                                            "data_quality_issue"], depth=1, token_budget=1500)
    routines = [c["routine"] for c in evidence["routines"]]
    team = team_from_query(query, sorted({c["team"] for c in evidence["routines"]}))
    in_scope = [c["routine"] for c in evidence["routines"] if c["team"] == team] or routines
    decisions = keeper.decisions(routines) if keeper else {}
    recalled = keeper.hydrate(query, in_scope) if keeper else {"memories": []}
    context = {
        "verified_findings": [c["text"] for c in (explained or {}).get("claims", [])],
        "source_conflicts": conflicts.get("assessments", []),
        "world_model": [{"type": e["type"], "name": e["name"], "attributes": e["attributes"]}
                        for e in world["entities"][:12]],
        "memories_from_earlier_runs": [f"[{m['layer']}] {m['content']}" for m in recalled["memories"]],
    }
    orchestrator.log("handoff", target_agent=pm.name, output_summary=
                     f"{len(evidence['routines'])} mined routines, {len(context['verified_findings'])} verified "
                     f"findings, {len(world['entities'])} World Model entities, {len(recalled['memories'])} memories, "
                     f"{len(decisions)} human decisions on record as context")
    plan = pm.plan(query, evidence, context, blocked=decisions)
    candidate = next(c for c in evidence["routines"] if c["routine"] == plan["chosen"])
    spec = plan["spec"]
    orchestrator.log("handoff", target_agent=engineer.name, output_summary=f"spec {spec['name']} ({plan['spec_origin']})")

    lessons = keeper.lessons_for(candidate["routine"], spec["summary"]) if keeper else None
    draft = engineer.build(spec, lessons)
    history = []
    for round_no in range(1, max_revisions + 2):
        report = qa.review(spec, draft, round_no=round_no)
        history.append({"round": round_no, "code_version": draft["version"], "code_hash": draft["code_hash"],
                        "passed": report["passed"], "pass_rate": report["pass_rate"],
                        "failed_checks": [c["name"] for c in report["checks"] if not c["passed"]],
                        "failure_purposes": sorted({f["purpose"] for f in report["failures"]})[:5]})
        if report["passed"] or round_no > max_revisions:
            break
        draft = engineer.revise(spec, draft, report)

    memo = manager.decide(query, plan, candidate, draft, report, history, prior=decisions.get(candidate["routine"]))
    approval = None
    if memo["decision"] == "approve_for_human_review":
        approval = approvals.request(run_id=ctx.run_id, automation=spec["name"], routine=candidate["routine"],
                                     code=draft["code"], report=report, decision_memo=memo,
                                     sample_mode=ctx.sample_mode)
        manager.log("request_approval", target_agent="Human", status="pending",
                    output_summary=f"{approval['approval_id']} for code {approval['code_hash'][:12]}",
                    code_hash=approval["code_hash"])
    run_ctx = [("run", ctx.run_id)]
    manager.remember([
        entity_observation("automation", spec["name"], source=manager.source(), contexts=run_ctx,
                           description=spec["summary"],
                           attributes={"code_hash": draft["code_hash"], "version": draft["version"],
                                       "qa_passed": report["passed"], "qa_pass_rate": report["pass_rate"],
                                       "decision": memo["decision"], "confidence": memo["confidence"],
                                       "approval_id": approval["approval_id"] if approval else None}),
        relationship_observation(("automation", spec["name"]), "automates", ("routine", candidate["routine"]),
                                 source=manager.source(), contexts=run_ctx),
    ], "automation decision")
    return {"plan": plan, "candidate": candidate, "spec": spec, "draft": draft, "report": report,
            "history": history, "memo": memo, "approval": approval,
            "memory": {"retrieved": [{k: m[k] for k in ("id", "content", "layer", "score", "retrievalReason")}
                                     for m in recalled["memories"]],
                       "human_decisions_applied": {r: m["content"] for r, m in decisions.items()}}}


def run_pipeline(user_query: str, context: dict, *, settings: Settings, tracer: Tracer,
                 generator: Optional[Generator], sample_mode: bool, store: WorldStoreService,
                 pipeline: IngestPipelineService, builder: ContextBuilderService, approvals: ApprovalLedger,
                 memory: Optional[MemoryStoreService] = None, recall: Optional[MemoryRetrievalService] = None,
                 max_revisions: int = 3, stages: Iterable[str] = STAGES, request_id: Optional[str] = None) -> dict:
    stages = [s for s in STAGES if s in set(stages)] or list(STAGES)
    data = build_data_source(settings, sample_mode)
    ctx = RunContext(run_id=tracer.run_id, settings=settings, tracer=tracer, generator=generator, data=data,
                     store=store, pipeline=pipeline, builder=builder, sample_mode=sample_mode,
                     memory=memory, recall=recall, max_revisions=max_revisions)
    keeper = MemoryKeeper(ctx) if memory is not None and recall is not None else None
    orchestrator = Orchestrator(ctx)
    ctx.agents_used.remove("Orchestrator")
    orchestrator.log("plan_run", input_summary=user_query, output_summary=
                     f"stages: {', '.join(stages)}; model: {ctx.model_name}; data: {data.name}")

    tables = catalog()
    discovered = explained = built = None
    conflicts: dict = {"conflicts": [], "assessments": []}
    try:
        if "discover" in stages:
            evidence, conflicts, discovered = _discover(ctx, user_query, tables)
        else:
            probe = Orchestrator(ctx)
            evidence = gather(lambda sql, purpose: probe.query(sql, purpose))
        if "explain" in stages:
            orchestrator.log("handoff", target_agent="ResearchPlannerAgent", output_summary=
                             f"{len(conflicts['assessments'])} source conflicts to account for")
            explained = explain(ctx, user_query, tables, conflicts)
        if "build" in stages:
            built = _build(ctx, orchestrator, keeper, user_query, evidence, explained, conflicts, approvals,
                           max_revisions)
    finally:
        close = getattr(data, "close", None)
        if close:
            close()
    mined = []
    if keeper:
        mined = keeper.mine({
            "run_id": ctx.run_id, "request_id": request_id, "routines": [r["routine"] for r in evidence["routines"]],
            "routine": built["candidate"]["routine"] if built else None,
            "automation": built["spec"]["name"] if built else None,
            "qa_rounds": built["history"] if built else [],
            "qa_failure_purposes": {h["round"]: h["failure_purposes"] for h in built["history"]} if built else {},
            "priority_issues": [i for issues in built["plan"]["priority_critiques"] for i in issues] if built else [],
            "conflict_assessments": conflicts.get("assessments", []),
            "insight_objections": [o for objs in (explained or {}).get("objections", []) for o in objs],
            "decision": built["memo"]["decision"] if built else None,
            "confidence": built["memo"]["confidence"] if built else None,
        })
    if "Orchestrator" in ctx.agents_used:
        ctx.agents_used.remove("Orchestrator")
    result = _result(ctx, stages, evidence, discovered, explained, built, conflicts, data.name)
    result["memory"] = {**(built["memory"] if built else {"retrieved": [], "human_decisions_applied": {}}),
                        "mined": mined}
    return result


def _result(ctx, stages, evidence, discovered, explained, built, conflicts, data_name) -> dict:
    findings = [c["text"] for c in (explained or {}).get("claims", [])]
    findings += [f"Data conflict: {a['task']} - {a['explanation']}" for a in conflicts.get("assessments", [])]
    for c in evidence["routines"]:
        reported = (f"timesheets report {c['reported_hours_per_week']:g} h/week"
                    if c["reported_hours_per_week"] is not None else "no timesheet category")
        logged = (f"tickets log {c['ticket_logged_hours_per_week']:g} h/week"
                  if c["ticket_logged_hours_per_week"] is not None else "not tracked in tickets")
        findings.append(f"{c['routine']}: {c['instances_per_week']:g} runs/week by {c['people']} people, "
                        f"{c['active_hours_per_week']:g} h/week hands-on in the activity log ({reported}, "
                        f"{logged}); risk {c['risk']}")

    evaluator_findings = []
    if explained:
        evaluator_findings += [f"insight critique round {i + 1}: {o}" for i, objs in enumerate(explained["objections"])
                               for o in objs]
    summary_parts = []
    if explained and explained["headline"]:
        summary_parts.append(explained["headline"])
    automation = None
    confidence = 0.5 if explained and explained["claims"] else 0.3
    if built:
        plan, report, memo, history, spec, draft = (built[k] for k in ("plan", "report", "memo", "history", "spec",
                                                                       "draft"))
        chosen = next(i for i in plan["ranking"] if i["routine"] == plan["chosen"])
        summary_parts.append(
            f"Automate {plan['chosen']} first: Priority Critic caps estimated savings at "
            f"{chosen['estimated_hours_saved_per_week']:g} measured hands-on h/week on the connected activity log "
            f"({chosen['risk']} risk; demo corpus when sample mode). The {spec['name']} automation "
            + (f"passed all {report['cases']} spec-derived QA cases" if report["passed"]
               else f"still failed QA ({', '.join(history[-1]['failed_checks'])})")
            + f" after {len(history)} QA round(s); Manager decision: {memo['decision']}.")
        evaluator_findings += [f"QA round {h['round']} (code v{h['code_version']}): "
                               + ("passed" if h["passed"] else "failed " + ", ".join(h["failed_checks"]))
                               for h in history]
        evaluator_findings += [f"priority critique: {issue}" for issues in plan["priority_critiques"]
                               for issue in issues]
        confidence = memo["confidence"]
        automation = {"name": spec["name"], "routine": plan["chosen"], "spec": spec, "spec_origin": plan["spec_origin"],
                      "code": draft["code"], "code_hash": draft["code_hash"], "version": draft["version"],
                      "origin": draft["origin"],
                      "qa": {k: report[k] for k in ("passed", "pass_rate", "cases", "model_cases", "checks",
                                                    "advisories")},
                      "qa_failures": report["failures"][:10]}
    evaluator_findings += [f"conflict critique: {a['task']} -> trust {a['trusted_source']}"
                           for a in conflicts.get("assessments", [])]

    return {
        "agents_used": list(ctx.agents_used),
        "result": {
            "summary": " ".join(summary_parts) or "No stage produced a result.",
            "findings": findings,
            "confidence": confidence,
            "ranking": built["plan"]["ranking"] if built else None,
            "decision": built["memo"] if built else None,
            "automation": automation,
        },
        "collaboration": {
            "feedback_loop_completed": True,
            "revision_count": (built["draft"]["version"] - 1) if built else 0,
            "qa_rounds": built["history"] if built else [],
            "priority_critique_rounds": built["plan"]["priority_rounds"] if built else 0,
            "insight_critique_rounds": explained["critique_rounds"] if explained else 0,
            "evaluator_findings": evaluator_findings,
        },
        "stages": {
            "discover": discovered,
            "explain": explained,
            "build": {k: built[k] for k in ("history",)} if built else None,
        },
        "approval": built["approval"] if built else None,
        "mode": {"sample_mode": ctx.sample_mode, "model": ctx.model_name, "data_source": data_name,
                 "stages": stages},
        "usage": {"model_calls": ctx.model_calls, "data_queries": ctx.queries},
    }
