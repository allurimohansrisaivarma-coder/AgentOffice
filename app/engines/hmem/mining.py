"""MemoryMiningService: turn runtime events into memory proposals.

Events mined: QA verdicts (what failed before code passed), Priority Critic
objections, Insight Critic objections, Conflict Critic trust decisions, the
run's decision, and human approval decisions. With a model, the Memory Keeper
agent words the lessons; without one, `direct_capture` writes direct summaries
(the reference engine's fallback). Every proposal still goes through the
store's approval boundary.
"""

from __future__ import annotations

from typing import Any, Optional

from app.engines.hmem.store import TYPES, new_record


def agent_source(agent: str) -> dict:
    return {"kind": "agent", "name": agent}


def approval_memory(approval: dict, routine: str) -> dict:
    """A human approval decision, captured directly (no model): durable, long-term preference."""
    decided = approval["status"]
    note = f": {approval['note']}" if approval.get("note") else ""
    return new_record(
        f"{approval['approver']} {decided} the {approval['automation']} automation for {routine} "
        f"(code {approval['code_hash'][:12]}){note}",
        type="preference", source={"kind": "human", "name": approval["approver"]}, confidence=0.95,
        domain=routine, tags=["approval", decided], layer="long_term",
        entities=[f"routine:{routine}", f"automation:{approval['automation']}"],
        derived_from=[approval["approval_id"]], goal_run_id=approval.get("run_id"),
        facts={"kind": "approval_decision", "key": f"approval:{routine}", "routine": routine,
               "automation": approval["automation"], "decision": decided, "code_hash": approval["code_hash"],
               "approver": approval["approver"], "note": approval.get("note", "")})


def direct_capture(run: dict) -> list[dict]:
    """Direct summaries of one run's events, no model involved."""
    out = []
    run_id, routine = run["run_id"], run.get("routine")
    for h_prev, h_next in zip(run.get("qa_rounds", []), run.get("qa_rounds", [])[1:]):
        if not h_prev["passed"]:
            failures = run.get("qa_failure_purposes", {}).get(h_prev["round"], [])
            about = f"; failing cases: {', '.join(failures[:3])}" if failures else ""
            out.append({"content": f"{run['automation']} code v{h_prev['code_version']} failed QA on "
                                   f"{', '.join(h_prev['failed_checks'])}{about}; "
                                   f"v{h_next['code_version']} " + ("passed." if h_next["passed"] else "still failed."),
                        "type": "factual", "domain": routine, "tags": ["qa", "lesson", *h_prev["failed_checks"]],
                        "confidence": 0.8, "refs": [f"{run_id}:qa_round_{h_prev['round']}"]})
    for issue in run.get("priority_issues", []):
        head = issue.split(":", 1)[0]
        domain = head if ":" in issue and head in run.get("routines", []) else "planning"
        out.append({"content": f"Priority Critic: {issue}. Size savings by measured hands-on time.",
                    "type": "factual", "domain": domain, "tags": ["priority", "lesson"],
                    "confidence": 0.75, "refs": [f"{run_id}:priority_critique"]})
    for assessment in run.get("conflict_assessments", []):
        out.append({"content": f"For {assessment['task']} hours, trust {assessment['trusted_source']}: "
                               f"{assessment['explanation']}",
                    "type": "factual", "domain": assessment["task"], "tags": ["data_quality", "trust"],
                    "confidence": 0.8, "refs": [f"{run_id}:conflict_critique"]})
    for objection in run.get("insight_objections", [])[:3]:
        out.append({"content": f"Insight Critic rejected a report claim: {objection}",
                    "type": "general", "domain": "reporting", "tags": ["insight", "lesson"],
                    "confidence": 0.6, "refs": [f"{run_id}:insight_critique"]})
    if run.get("decision"):
        out.append({"content": f"Run {run_id} chose {routine}; Manager decision {run['decision']} "
                               f"(confidence {run['confidence']}).",
                    "type": "general", "domain": routine, "tags": ["decision"], "confidence": 0.7,
                    "refs": [run_id]})
    return out


def validate_candidates(obj: Any, domains: set[str]) -> list[str]:
    if not isinstance(obj, dict) or not isinstance(obj.get("memories"), list):
        return ["reply must be {\"memories\": [...]}"]
    errors = []
    for i, m in enumerate(obj["memories"][:6]):
        if not isinstance(m, dict) or not str(m.get("content", "")).strip():
            errors.append(f"memories[{i}].content is required")
            continue
        if len(str(m["content"])) > 300:
            errors.append(f"memories[{i}].content must be at most 300 characters")
        if m.get("type") not in TYPES or m.get("type") == "personal":
            errors.append(f"memories[{i}].type must be factual, preference or general")
        if m.get("domain") not in domains:
            errors.append(f"memories[{i}].domain must be one of {sorted(domains)}")
        c = m.get("confidence")
        if not isinstance(c, (int, float)) or not 0 < c <= 1:
            errors.append(f"memories[{i}].confidence must be in (0, 1]")
    return errors


def to_record(candidate: dict, *, agent: str, run_id: str, refs: list[str], mission_id: Optional[str] = None) -> dict:
    domain = candidate["domain"]
    return new_record(candidate["content"], type=candidate["type"], source=agent_source(agent),
                      confidence=candidate["confidence"], domain=domain, tags=candidate.get("tags", []),
                      entities=[f"routine:{domain}"] if domain not in ("reporting", "planning") else [],
                      derived_from=refs, goal_run_id=run_id, mission_id=mission_id,
                      facts={"kind": "lesson", "run_id": run_id})
