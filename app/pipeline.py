"""The AgentOffice run: evidence from the data layer, then the stage 3 office loop.

    PM -> Priority Critic -> PM (plan + spec) -> Engineer -> QA -> Engineer (revise, capped) -> QA -> Manager
    -> human approval request (bound to the code hash)

Every step is traced; everything learned is written to the World Model.
"""

from __future__ import annotations

from typing import Optional

from app.agents.base import Agent, RunContext
from app.agents.stage3 import Engineer, Manager, ProductManager, QAAgent
from app.analysis.routines import gather
from app.approvals import ApprovalLedger
from app.config import Settings
from app.data import build_data_source
from app.engines.world_model import (
    ContextBuilderService, IngestPipelineService, WorldStoreService, entity_observation, relationship_observation,
)
from app.llm import Generator
from app.tracing import Tracer


class Orchestrator(Agent):
    name = "Orchestrator"


def run_pipeline(user_query: str, context: dict, *, settings: Settings, tracer: Tracer,
                 generator: Optional[Generator], sample_mode: bool, store: WorldStoreService,
                 pipeline: IngestPipelineService, builder: ContextBuilderService,
                 approvals: ApprovalLedger, max_revisions: int = 3) -> dict:
    data = build_data_source(settings, sample_mode)
    ctx = RunContext(run_id=tracer.run_id, settings=settings, tracer=tracer, generator=generator, data=data,
                     store=store, pipeline=pipeline, builder=builder, sample_mode=sample_mode,
                     max_revisions=max_revisions)
    orchestrator = Orchestrator(ctx)
    ctx.agents_used.remove("Orchestrator")
    orchestrator.log("plan_run", input_summary=user_query, output_summary=
                     f"model: {ctx.model_name}; data: {data.name}; world model: {store.path}")

    pm, engineer, qa, manager = ProductManager(ctx), Engineer(ctx), QAAgent(ctx), Manager(ctx)
    try:
        evidence = gather(lambda sql, purpose: pm.query(sql, purpose))
        world_context = builder.build(user_query, types=["routine", "automation_candidate", "automation"],
                                      depth=1, token_budget=1500)
        orchestrator.log("handoff", target_agent=pm.name, output_summary=
                         f"{len(evidence['routines'])} mined routines, {len(world_context['entities'])} World Model "
                         f"entities as context")
        plan = pm.plan(user_query, evidence, world_context)
        candidate = next(c for c in evidence["routines"] if c["routine"] == plan["chosen"])
        spec = plan["spec"]
        orchestrator.log("handoff", target_agent=engineer.name, output_summary=f"spec {spec['name']} "
                         f"({plan['spec_origin']})")

        draft = engineer.build(spec)
        history = []
        for round_no in range(1, max_revisions + 2):
            report = qa.review(spec, draft, round_no=round_no)
            history.append({"round": round_no, "code_version": draft["version"], "code_hash": draft["code_hash"],
                            "passed": report["passed"], "pass_rate": report["pass_rate"],
                            "failed_checks": [c["name"] for c in report["checks"] if not c["passed"]]})
            if report["passed"] or round_no > max_revisions:
                break
            draft = engineer.revise(spec, draft, report)

        memo = manager.decide(user_query, plan, candidate, draft, report, history)
        approval = None
        if memo["decision"] == "approve_for_human_review":
            approval = approvals.request(run_id=ctx.run_id, automation=spec["name"], code=draft["code"],
                                         report=report, decision_memo=memo)
            manager.log("request_approval", target_agent="Human", status="pending", output_summary=
                        f"{approval['approval_id']} for code {approval['code_hash'][:12]}")
    finally:
        close = getattr(data, "close", None)
        if close:
            close()

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

    findings = []
    for c in evidence["routines"]:
        reported = (f"timesheets report {c['reported_hours_per_week']:g} h/week"
                    if c["reported_hours_per_week"] is not None else "no timesheet category")
        logged = (f"tickets log {c['ticket_logged_hours_per_week']:g} h/week"
                  if c["ticket_logged_hours_per_week"] is not None else "not tracked in tickets")
        findings.append(f"{c['routine']}: {c['instances_per_week']:g} runs/week by {c['people']} people, "
                        f"{c['active_hours_per_week']:g} h/week hands-on in the activity log ({reported}, "
                        f"{logged}); risk {c['risk']}")
    chosen = next(i for i in plan["ranking"] if i["routine"] == plan["chosen"])
    summary = (f"Automate {plan['chosen']} first: about {chosen['estimated_hours_saved_per_week']:g} h/week "
               f"saved ({chosen['risk']} risk). The {spec['name']} automation "
               + (f"passed all {report['cases']} spec-derived QA cases" if report["passed"]
                  else f"still failed QA ({', '.join(history[-1]['failed_checks'])})")
               + f" after {len(history)} QA round(s); Manager decision: {memo['decision']}.")
    evaluator_findings = [f"round {h['round']} (code v{h['code_version']}): "
                          + ("passed" if h["passed"] else "failed " + ", ".join(h["failed_checks"]))
                          for h in history]
    evaluator_findings += [f"priority critique: {issue}" for round_issues in plan["priority_critiques"]
                           for issue in round_issues]
    return {
        "agents_used": list(ctx.agents_used),
        "result": {
            "summary": summary,
            "findings": findings,
            "confidence": memo["confidence"],
            "ranking": plan["ranking"],
            "decision": memo,
            "automation": {"name": spec["name"], "routine": plan["chosen"], "spec": spec,
                           "spec_origin": plan["spec_origin"], "code": draft["code"], "code_hash": draft["code_hash"],
                           "version": draft["version"], "origin": draft["origin"],
                           "qa": {k: report[k] for k in ("passed", "pass_rate", "cases", "model_cases", "checks",
                                                         "advisories")},
                           "qa_failures": report["failures"][:10]},
        },
        "collaboration": {
            "feedback_loop_completed": True,
            "revision_count": draft["version"] - 1,
            "qa_rounds": history,
            "priority_critique_rounds": plan["priority_rounds"],
            "evaluator_findings": evaluator_findings,
        },
        "approval": approval,
        "mode": {"sample_mode": sample_mode, "model": ctx.model_name, "data_source": data.name},
        "usage": {"model_calls": ctx.model_calls, "data_queries": ctx.queries},
    }
