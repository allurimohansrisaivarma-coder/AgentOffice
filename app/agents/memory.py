"""Memory Keeper: the office's long memory (H-MEM engine).

At the start of the build stage it hydrates the run with memories retrieved
for the question and the candidate routines, and returns the human approval
decisions on record, which the PM and Manager must respect. At the end of the
run it mines the run's events (QA verdicts, critiques, trust decisions,
outcome) into memory proposals; every proposal passes the store's approval
boundary. Human approve/reject decisions are captured directly, no model.
"""

from __future__ import annotations

import json

from app.adapters.http import AdapterError
from app.agents.base import OFFLINE_MODEL, Agent, AgentError
from app.engines.hmem import MemoryStoreService
from app.engines.hmem.mining import approval_memory, direct_capture, to_record, validate_candidates


def record_approval(store: MemoryStoreService, approval: dict) -> dict:
    """Called when a human decides on an approval request (outside any run)."""
    routine = approval.get("routine") or "unknown"
    return store.propose(approval_memory(approval, routine), actor=f"human:{approval['approver']}")


class MemoryKeeper(Agent):
    name = "MemoryKeeperAgent"
    tier = "fast"

    SYSTEM = ("You are the Memory Keeper agent in AgentOffice. From this run's events, write at most 5 short, "
              "durable lessons that would help the team next time (what failed and why, which source to trust, "
              "how to size savings). No personal data. Reply with only JSON: {\"memories\": [{\"content\": "
              "<= 300 chars, \"type\": \"factual\" | \"preference\" | \"general\", \"domain\": <one of the given "
              "domains>, \"tags\": [...], \"confidence\": 0..1}]}.")

    def decisions(self, routines: list[str]) -> dict[str, dict]:
        """The latest active human approval decision per routine."""
        latest = {}
        for m in self.ctx.memory.find(fact_kind="approval_decision"):
            if m["facts"]["routine"] in routines:
                latest[m["facts"]["routine"]] = m
        return latest

    def hydrate(self, query: str, routines: list[str], top_n: int = 5) -> dict:
        """One scoped retrieval per candidate routine (its domain + entity), merged and re-ranked."""
        merged: dict[str, dict] = {}
        considered = 0
        for routine in routines:
            payload = self.ctx.recall.retrieve(f"{query} {routine.replace('_', ' ')}", domains=[routine],
                                               entities=[f"routine:{routine}"], top_n=2, actor=self.name,
                                               strategy="per-candidate", record_reads=False)
            considered = payload["considered"]
            for m in payload["memories"]:
                if m["id"] not in merged or m["score"] > merged[m["id"]]["score"]:
                    merged[m["id"]] = m
        memories = sorted(merged.values(), key=lambda m: (-m["score"], m["id"]))[:top_n]
        if memories:
            self.ctx.memory.touch([m["id"] for m in memories], actor=self.name, reason=f"recalled for: {query[:120]}")
        self.log("hmem_retrieve", target_agent="PMAgent", input_summary=query, output_summary=[
            {"id": m["id"], "score": m["score"], "layer": m["layer"], "reason": m["retrievalReason"],
             "content": m["content"][:120]} for m in memories] or "no memories above the threshold",
                 considered=considered, strategy="per-candidate")
        return {"strategy": "per-candidate", "memories": memories, "considered": considered}

    def lessons_for(self, routine: str, spec_summary: str) -> dict:
        payload = self.ctx.recall.retrieve(spec_summary, domains=[routine], entities=[f"routine:{routine}"],
                                           tags=["qa", "lesson"], actor=self.name, top_n=3)
        if payload["memories"]:
            self.log("hmem_retrieve", target_agent="EngineerAgent", input_summary=spec_summary,
                     output_summary=[m["content"][:120] for m in payload["memories"]])
        return payload

    def mine(self, digest: dict) -> list[dict]:
        domains = set(digest.get("routines", [])) | {"reporting", "planning"} | {
            a["task"] for a in digest.get("conflict_assessments", [])}
        direct = direct_capture(digest)
        refs = sorted({r for c in direct for r in c["refs"]}) or [digest["run_id"]]
        if self.offline or not direct:
            candidates = direct
            self.log("mine_memories", model=OFFLINE_MODEL, output_summary=f"{len(direct)} direct-capture proposals")
        else:
            try:
                obj = self.chat_json(self.SYSTEM, json.dumps({"domains": sorted(domains), "events": direct},
                                                             indent=1, default=str),
                                     action="mine_memories", validate=lambda o: validate_candidates(o, domains))
                candidates = [{**m, "refs": refs} for m in obj["memories"][:5]]
            except (AgentError, AdapterError) as e:   # optional model call: degrade, never fail the run
                candidates = direct
                self.log("mine_memories", status="error", output_summary=f"model mining unusable, falling back to "
                                                                         f"direct capture: {e.message}")
        results = []
        for c in candidates:
            try:
                record = to_record(c, agent=self.name, run_id=digest["run_id"], refs=c.get("refs") or refs,
                                   mission_id=digest.get("request_id"))
            except ValueError as e:
                results.append({"status": "rejected", "reason": str(e), "content": c.get("content", "")[:120]})
                continue
            outcome = self.ctx.memory.propose(record, actor=self.name)
            results.append({"status": outcome["status"], "reason": outcome["reason"], "content": record["content"],
                            "id": record["id"], "conflicts": len(outcome["conflicts"])})
        self.log("hmem_write", target_agent="H-MEM", output_summary=[
            f"{r['status']}: {r['content'][:90]}" for r in results] or "nothing to remember")
        return results
