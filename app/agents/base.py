"""Shared agent plumbing: the run context, traced model calls, traced data queries.

Every model call goes through `Agent.chat` (traced with the model that actually
answered, capped per run). Every data query goes through `Agent.query` (traced
as a Zetaris call, or as a skipped Zetaris call plus a sample-SQL query in
sample mode). In sample mode there is no generator: agents use their own
deterministic, data-driven fallback and log `model: offline-fallback`.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.adapters.http import AdapterError
from app.config import Settings
from app.data import DataSource
from app.engines.hmem import MemoryRetrievalService, MemoryStoreService
from app.engines.world_model import ContextBuilderService, IngestPipelineService, WorldStoreService, agent_source
from app.llm import Generator
from app.tracing import Tracer

OFFLINE_MODEL = "offline-fallback"
# Reasoning models (Nemotron via Nightshift) spend part of the budget thinking before they answer.
MAX_TOKENS = 8000


class AgentError(Exception):
    """An agent could not produce a usable result; carries a structured error."""

    def __init__(self, agent: str, type: str, message: str, recoverable: bool = False):
        super().__init__(f"{agent}: {message}")
        self.agent, self.type, self.message, self.recoverable = agent, type, message, recoverable


@dataclass
class RunContext:
    run_id: str
    settings: Settings
    tracer: Tracer
    generator: Optional[Generator]
    data: DataSource
    store: WorldStoreService
    pipeline: IngestPipelineService
    builder: ContextBuilderService
    sample_mode: bool
    memory: Optional[MemoryStoreService] = None
    recall: Optional[MemoryRetrievalService] = None
    max_revisions: int = 3
    max_model_calls: int = 40
    model_calls: int = 0
    queries: int = 0
    agents_used: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)   # agents may call the model in parallel

    @property
    def model_name(self) -> str:
        return self.generator.name if self.generator else OFFLINE_MODEL

    def lane(self, tier: str) -> dict:
        """Request parameters for a tier of work (only Meterless has lanes; other providers use their one model):
        'fast'  - short structured jobs (routing, test design, reviews, memos): the fast model, thinking off;
        'draft' - first plans and code: the same fast lane (measured: Lightning with thinking on was slower
                  than Ultra on a full plan), escalated to 'deep' when rejected;
        'deep'  - work a critic or QA rejected: the configured route (Nemotron 3 Ultra)."""
        fast = (self.settings.meterless_fast_model or "").strip()
        if self.settings.llm_provider != "meterless" or tier == "deep" or fast.lower() in ("", "off", "none", "route"):
            return {}
        if self.settings.meterless_fast_thinking:
            return {"model": fast}
        return {"model": fast, "chat_template_kwargs": {"enable_thinking": False}}


def strip_reasoning(text: str) -> str:
    """Drop a reasoning model's thinking. Nemotron via Nightshift may send the thinking
    with only a closing </think>, so keep whatever follows the last one."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


def extract_json(text: str) -> Any:
    text = strip_reasoning(text)
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the reply")
    try:
        return json.loads(candidate[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"reply is not valid JSON: {e.msg} at character {e.pos}") from None


def extract_code(text: str) -> str:
    text = strip_reasoning(text)
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    return (max(blocks, key=len) if blocks else text).strip() + "\n"


def _unavailable(e: AdapterError) -> bool:
    """The provider has no such model/function right now (NVIDIA answers 404 'Function ... Not found')."""
    return e.status_code == 404 or "404" in e.message or "not found" in e.message.lower()


class Agent:
    name = "Agent"
    tier = "deep"    # which Nightshift lane this agent's calls use by default; see RunContext.model_for

    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        if self.name not in ctx.agents_used:
            ctx.agents_used.append(self.name)

    @property
    def offline(self) -> bool:
        return self.ctx.generator is None

    def log(self, action: str, **fields) -> dict:
        return self.ctx.tracer.event(self.name, action, **fields)

    # ---------------------------------------------------------------- model

    def chat(self, system: str, user: str, *, action: str, target_agent: Optional[str] = None,
             temperature: float = 0.2, max_tokens: int = MAX_TOKENS, retry_count: int = 0,
             tier: Optional[str] = None) -> str:
        ctx = self.ctx
        if ctx.generator is None:
            raise AgentError(self.name, "no_model", "no model available (sample mode)")
        tier = tier or self.tier
        params = ctx.lane(tier)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for attempt in (1, 2):
            with ctx.lock:
                if ctx.model_calls >= ctx.max_model_calls:
                    raise AgentError(self.name, "model_budget",
                                     f"model call cap of {ctx.max_model_calls} per run reached")
                ctx.model_calls += 1
            started = time.monotonic()
            try:
                result = ctx.generator.chat(messages, temperature=temperature, max_tokens=max_tokens, **params)
                break
            except AdapterError as e:
                model = params.get("model")
                self.log(action, status="error", model=model or ctx.generator.name, input_summary=user,
                         output_summary=e.message, error_type=e.to_dict()["type"], retry_count=retry_count,
                         tier=tier, elapsed_s=round(time.monotonic() - started, 2))
                fallback = ctx.settings.meterless_fallback_model if ctx.settings.llm_provider == "meterless" else ""
                if attempt == 1 and _unavailable(e) and fallback and fallback != model:
                    self.log("model_fallback", status="retry", target_agent=self.name, output_summary=
                             f"{model or 'the route'} is unavailable at the provider; retrying once on {fallback}")
                    params = dict(params, model=fallback)
                    continue
                if attempt == 1 and params and tier != "deep" and "timeout" not in e.type:
                    # e.g. the fast model's empty_response: the deep lane gets one try
                    self.log("model_fallback", status="retry", target_agent=self.name, output_summary=
                             f"the {tier} lane failed ({e.message[:120]}); retrying once on the deep lane")
                    params, tier = ctx.lane("deep"), "deep"
                    continue
                raise
        self._cut_off = result.finish_reason == "length"
        self.log(action, model=result.model, input_summary=user, output_summary=result.content,
                 target_agent=target_agent, retry_count=retry_count, tokens=result.usage.get("total_tokens"),
                 provider=ctx.generator.name, tier=tier, elapsed_s=round(time.monotonic() - started, 2),
                 status="truncated" if self._cut_off else "ok")
        return result.content

    def chat_json(self, system: str, user: str, *, action: str, validate: Callable[[Any], list[str]],
                  attempts: int = 2, **kwargs) -> Any:
        prompt = user
        errors: list[str] = []
        for attempt in range(attempts):
            text = self.chat(system, prompt, action=action, retry_count=attempt, **kwargs)
            try:
                obj = extract_json(text)
                errors = validate(obj)
            except ValueError as e:
                errors = [str(e)]
                if getattr(self, "_cut_off", False):
                    errors = ["the reply was cut off at the length limit before the JSON ended: "
                              "think briefly and keep the JSON compact"]
            if not errors:
                return obj
            self.log(f"{action}_rejected", status="retry" if attempt + 1 < attempts else "error",
                     output_summary=errors[:6], retry_count=attempt)
            prompt = (user + "\n\nYour previous reply could not be used:\n- " + "\n- ".join(errors[:8])
                      + "\nReply again with only the corrected JSON object.")
        raise AgentError(self.name, "invalid_model_output", "; ".join(errors[:4]), recoverable=True)

    # ---------------------------------------------------------------- data

    def query(self, sql: str, purpose: str, *, limit: int = 20000) -> list[dict]:
        ctx = self.ctx
        ctx.queries += 1
        started = time.monotonic()
        target = "Zetaris" if not ctx.sample_mode else "SampleSQL"
        if ctx.sample_mode:
            self.log("zetaris_query", status="skipped", target_agent="Zetaris", input_summary=sql,
                     output_summary="zetaris: skipped (sample mode); running the same SQL on the sample data")
        try:
            rows = ctx.data.run_sql(sql, limit=limit)
        except AdapterError as e:
            self.log("data_query", status="error", target_agent=target, input_summary=sql,
                     output_summary=e.message, error_type=e.to_dict()["type"], purpose=purpose)
            raise
        executed = getattr(ctx.data, "last_executed", None)
        extra = {"executed_sql": executed} if executed and executed != sql else {}
        self.log("data_query", target_agent=target, input_summary=sql, output_summary=f"{len(rows)} rows",
                 purpose=purpose, data_source=ctx.data.name, elapsed_s=round(time.monotonic() - started, 2),
                 **extra)
        return rows

    # ---------------------------------------------------------------- world model

    def source(self) -> dict:
        return agent_source(self.name, run_id=self.ctx.run_id)

    def data_source(self, table: str, rows: list) -> dict:
        system = "sample" if self.ctx.sample_mode else "zetaris"
        return {"system": system, "table": table, "rows": list(rows)[:50]}

    def remember(self, observations: list[dict], what: str) -> dict:
        result = self.ctx.pipeline.ingest(observations, actor=self.name)
        self.log("world_model_write", target_agent="WorldModel", output_summary=
                 f"{what}: {result.get('accepted', 0)} observations, {len(result.get('diff', []))} records changed",
                 rejected=len(result.get("rejected", [])))
        return result
