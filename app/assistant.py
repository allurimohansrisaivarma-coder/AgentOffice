"""Workspace help: answer a question from the company's data (or the attached file), or draft an email.

    Assistant (plan)     works out which facts the request needs, as sub-questions the data can answer
    Retriever (stage 2)  one read-only SQL query per sub-question on Zetaris; SQL errors go back to the model (capped)
    Assistant (write)    the answer or the email, from those rows only. Personal values (names, emails, phones)
                         reach the model as placeholders like [[P1]] and are filled back in locally afterwards
    Grounding Critic     rules first: every number must come from the rows or the user's message, every
                         placeholder must be one that was issued, and an email's recipients must be people from
                         the rows or the request, never IDs. When the rules pass, a model review checks that each
                         figure is tied to the right week, person or category. Objections go back (capped)

Nothing is sent anywhere: an email is a draft the user copies. Sample mode has no model, so it says so
instead of guessing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.adapters.http import AdapterError
from app.agents.base import Agent, AgentError, RunContext
from app.agents.stage2 import _NUMBER, Retriever, _supported
from app.config import Settings
from app.data import UPLOAD_CELLS, UPLOAD_CELLS_TYPES, build_data_source, catalog
from app.engines.world_model import ContextBuilderService, IngestPipelineService, WorldStoreService
from app.llm import Generator
from app.tasks import files
from app.tracing import Tracer

KINDS = ("question", "email")
MAX_ROUNDS = 2
_TOKEN = re.compile(r"\[\[P\d+\]\]")
_EMAIL_ANY = re.compile(r"[^@\s<>\"'(),;:\[\]]+@[^@\s<>\"'(),;:\[\]]+\.[A-Za-z]{2,}")
_PERSONAL_COL = re.compile(r"(?i)(name|e-?mail|phone|mobile|address)")


# ------------------------------------------------------------------ personal values

def mask_rows(evidence: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Evidence with personal values replaced by [[Pn]] placeholders, and the placeholder -> value map.
    The same value always gets the same placeholder, so the model can still tell people apart."""
    tokens: dict[str, str] = {}
    by_value: dict[str, str] = {}
    masked = []
    for e in evidence:
        rows = []
        for r in e["rows"]:
            row = {}
            for c, v in r.items():
                if isinstance(v, str) and v.strip() and (_PERSONAL_COL.search(c) or _EMAIL_ANY.fullmatch(v.strip())):
                    if v not in by_value:
                        by_value[v] = f"[[P{len(by_value) + 1}]]"
                        tokens[by_value[v]] = v
                    v = by_value[v]
                row[c] = v
            rows.append(row)
        masked.append({**e, "rows": rows})
    return masked, tokens


def unmask(text: str, tokens: dict[str, str]) -> str:
    return _TOKEN.sub(lambda m: tokens.get(m.group(0), m.group(0)), text)


# ------------------------------------------------------------------ the Grounding Critic's rules

def _numbers_in(text: str) -> list[float]:
    return [float(raw.replace(",", "")) for raw in _NUMBER.findall(text)]


def critique_grounding(text: str, evidence: list[dict], message: str, tokens: dict[str, str]) -> list[str]:
    """Objections to anything in a draft that the (masked) rows or the user's own message do not support."""
    objections = []
    for token in sorted(set(_TOKEN.findall(text)) - set(tokens)):
        objections.append(f"{token} is not a placeholder from the rows; use only the ones given")
    for address in sorted(set(_EMAIL_ANY.findall(text)) - set(_EMAIL_ANY.findall(message))):
        objections.append(f"the address {address} is not in the rows or the request; write people as their "
                          "[[Pn]] placeholders")
    cells_text = [str(v) for e in evidence for r in e["rows"] for v in r.values() if v is not None]
    known = {n for t in [message, *cells_text] for n in _numbers_in(t)}   # every number written in a cell or the ask
    cells = [float(v) for e in evidence for r in e["rows"] for v in r.values()
             if isinstance(v, (int, float)) and not isinstance(v, bool)]  # numeric cells, for ratios and percentages
    for raw in dict.fromkeys(_NUMBER.findall(_TOKEN.sub(" ", text))):   # placeholders' digits are not figures
        value = float(raw.replace(",", ""))
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        if value not in known and not _supported(value, decimals, cells):
            objections.append(f"the number {raw} is not in the rows or the request, nor a ratio or percentage of "
                               "two result cells; use the figures given or leave a gap like [amount]")
    return objections


def critique_recipients(to: list[str], message: str) -> list[str]:
    """An email goes to people from the rows (as their placeholders) or to whoever the user named; an ID such
    as a person_id is not a recipient."""
    return [f"recipient {r!r} is not a person from the rows or the request; put their email placeholder "
            "(e.g. [[P2]]) in 'to', or leave 'to' empty" for r in (t.strip() for t in to)
            if not (_TOKEN.fullmatch(r) or (r and r.casefold() in message.casefold()))]


# ------------------------------------------------------------------ agents

TRUNCATION_RULE = ("A result marked \"truncated\": true holds only its first 30 rows: never state a total, maximum, "
                   "ranking or 'every week' from it, and say the list may be incomplete. ")


def _results(evidence: list[dict]) -> list[dict]:
    return [{"query": e["id"], "question": e["question"], "rows": e["rows"],
             **({"truncated": True} if e.get("truncated") else {})} for e in evidence]


class Assistant(Agent):
    name = "AssistantAgent"

    PLAN_SYSTEM = ("You are the Assistant agent in AgentOffice, helping an office worker with a daily request: a "
                   "question about their company's workforce data or their attached file, or an email to draft. "
                   "List the facts the request needs as 0 to 3 sub-questions that ONE read-only SQL query each can "
                   "answer over the catalog. Use 0 when the request needs no company data (e.g. a general email). "
                   "For an email to people in the data (a team, a role, named staff), one sub-question must fetch "
                   "their name and email, so the draft can address them. "
                   "Reply with only JSON: {\"sub_questions\": [{\"question\": \"...\"}]}.")
    WRITE_SYSTEM = {
        "question": ("You are the Assistant agent in AgentOffice. Answer the user's question from the query results "
                     "only, in one to four short sentences (a short list is fine). Every number must be copied from "
                     "the results (rounded is fine) or be a ratio or percentage of two of them. If the results do "
                     "not answer the question, say what is missing; never guess. People appear as placeholders "
                     "like [[P3]]: write them exactly as given. Plain text. Reply with only JSON: "
                     "{\"answer\": \"...\"}."),
        "email": ("You are the Assistant agent in AgentOffice. Draft the email the user asked for, using ONLY facts "
                  "from the query results and the user's message; never invent numbers, dates, amounts or people. "
                  "People appear as placeholders like [[P3]]: write them exactly as given, never a guessed name or "
                  "address. If the email needs a fact the results lack, leave a visible gap such as [amount]. "
                  "'to' lists each recipient's email placeholder from the results (e.g. [[P4]]) or addresses the "
                  "user wrote; never an ID such as a person_id, and leave it empty if the results hold no addresses. "
                  "Plain text, no markdown, a greeting and a sign-off. Reply with only JSON: "
                  "{\"to\": [<email placeholders or recipients the user named>], \"subject\": \"...\", \"body\": \"...\"}."),
    }

    def plan(self, message: str, kind: str, cat: list[dict]) -> list[dict]:
        def validate(obj: Any) -> list[str]:
            subs = obj.get("sub_questions") if isinstance(obj, dict) else None
            if not isinstance(subs, list) or len(subs) > 3:
                return ["reply must be {\"sub_questions\": [...]} with 0 to 3 items"]
            if any(not isinstance(s, dict) or not str(s.get("question", "")).strip() for s in subs):
                return ["every sub-question needs a non-empty 'question'"]
            return []

        obj = self.chat_json(self.PLAN_SYSTEM, f"Request ({kind}): {message}\n\nCatalog:\n{json.dumps(cat, indent=1)}",
                             action="plan_help", target_agent="RetrieverAgent", validate=validate)
        return [{"id": f"q{i + 1}", "question": str(s["question"]).strip()} for i, s in enumerate(obj["sub_questions"])]

    def write(self, message: str, kind: str, evidence: list[dict], *, objections: Optional[list[str]] = None,
              previous: Optional[dict] = None) -> dict:
        def validate(obj: Any) -> list[str]:
            if not isinstance(obj, dict):
                return ["reply must be a JSON object"]
            if kind == "question":
                return [] if isinstance(obj.get("answer"), str) and obj["answer"].strip() else ["'answer' must be text"]
            errors = [f"'{k}' must be non-empty text" for k in ("subject", "body")
                      if not isinstance(obj.get(k), str) or not obj[k].strip()]
            if not isinstance(obj.get("to"), list) or not all(isinstance(t, str) for t in obj["to"]):
                errors.append("'to' must be a list of strings (may be empty)")
            return errors

        results = _results(evidence)
        prompt = (f"User's request: {message}\n\nQuery results:\n{json.dumps(results, indent=1, default=str)}"
                  if results else f"User's request: {message}\n\nNo company data was needed or found for this.")
        if objections:
            prompt += ("\n\nYour previous draft:\n" + json.dumps(previous, default=str) + "\n\nThe Grounding Critic "
                       "objected:\n- " + "\n- ".join(objections) + "\nFix those points and reply with the full JSON.")
        action = ("revise_" if objections else "write_") + ("answer" if kind == "question" else "email")
        return self.chat_json(self.WRITE_SYSTEM[kind] + " " + TRUNCATION_RULE, prompt, action=action,
                              target_agent="GroundingCritic", validate=validate, temperature=0.3)


class GroundingCritic(Agent):
    name = "GroundingCritic"

    # Rules prove every figure and person exists in the rows; this review checks they are attached to the right
    # row: the right week, person or category, ties and groupings as the rows show them.
    REVIEW_SYSTEM = ("You are the Grounding Critic agent in AgentOffice. Every number and person in this draft has "
                     "already been found in the query results. Check each statement against the rows it relies on: "
                     "the right figure for the right week, person or category; ties, groupings and ranges stated "
                     "exactly as the rows show; nothing claimed about rows that are not shown. " + TRUNCATION_RULE +
                     "Object only to concrete mismatches, quoting the statement and the row values that contradict "
                     "it. Reply with only JSON: {\"objections\": [{\"statement\": \"...\", \"reason\": \"...\"}]} "
                     "(an empty list if every statement matches).")

    def _review_wording(self, text: str, evidence: list[dict], message: str) -> list[str]:
        prompt = (f"User's request: {message}\n\nQuery results:\n{json.dumps(_results(evidence), indent=1, default=str)}"
                  f"\n\nDraft:\n{text}")
        try:
            obj = self.chat_json(self.REVIEW_SYSTEM, prompt, action="review_wording", target_agent="AssistantAgent",
                                 attempts=1, validate=lambda o: [] if isinstance(o, dict) and isinstance(
                                     o.get("objections"), list) else ["reply must be {\"objections\": [...]}"])
        except (AgentError, AdapterError) as e:   # optional model call: degrade, never fail the run
            self.log("review_wording", status="error", output_summary=f"wording review skipped: {e.message}")
            return []
        return [f"{str(o.get('statement', ''))[:200]!r}: {str(o.get('reason', ''))[:300]}"
                for o in obj["objections"][:5] if isinstance(o, dict)]

    def review(self, text: str, evidence: list[dict], message: str, tokens: dict[str, str], round_no: int, *,
               recipients: Optional[list[str]] = None) -> list[str]:
        objections = critique_grounding(text, evidence, message, tokens)
        objections += critique_recipients(recipients or [], message)
        if not objections and evidence and not self.offline:
            objections = self._review_wording(text, evidence, message)
        self.log("critique_grounding", target_agent="AssistantAgent", retry_count=round_no - 1,
                 status="needs_revision" if objections else "approved",
                 output_summary=objections or "every number and person traces to the data or your request, and "
                                              "each statement matches its rows")
        return objections


# ------------------------------------------------------------------ the run

def draft_text(kind: str, draft: dict) -> str:
    if kind == "question":
        return draft["answer"]
    return "\n".join([draft["subject"], *draft["to"], draft["body"]])


def upload_catalog_entry(upload_id: str, meta: dict, parsed: dict) -> dict:
    sheets = {s["name"]: s["columns"] for s in parsed["sheets"]}
    return {"table": UPLOAD_CELLS, "ref": f"workforce.{UPLOAD_CELLS}", "rows": sum(len(s["rows"]) * len(s["columns"])
                                                                              for s in parsed["sheets"]),
            "columns": [{"name": c, "type": t} for c, t in UPLOAD_CELLS_TYPES.items()],
            "note": (f"The user's attached file {meta['filename']!r}, one row per cell. Always filter "
                     f"upload_id = '{upload_id}'. Sheets and their columns (col_name values): {json.dumps(sheets)}. "
                     "A record is all cells sharing sheet_name and row_no; cell_value is text, so CAST it for maths.")}


def assist(kind: str, message: str, upload_id: Optional[str], *, settings: Settings, tracer: Tracer,
           generator: Optional[Generator], sample_mode: bool, store: WorldStoreService,
           pipeline: IngestPipelineService, builder: ContextBuilderService, uploads: files.UploadStore) -> dict:
    if kind not in KINDS:
        raise AgentError("AssistantAgent", "bad_kind", f"unknown request kind {kind!r}")
    if generator is None:
        raise AgentError("AssistantAgent", "needs_model", "answering workspace questions and drafting emails needs "
                         "the model: connect Meterless Nightshift and turn sample mode off (sample mode runs the "
                         "bundled spreadsheet demos and the time analysis)", recoverable=True)
    meta = uploads.meta(upload_id) if upload_id else None
    parsed = uploads.parsed(upload_id) if upload_id else None
    data = build_data_source(settings, sample_mode,
                             upload_cells=uploads.all_cells(include=upload_id) if sample_mode and parsed else None)
    ctx = RunContext(run_id=tracer.run_id, settings=settings, tracer=tracer, generator=generator, data=data,
                     store=store, pipeline=pipeline, builder=builder, sample_mode=sample_mode)
    cat = catalog() + ([upload_catalog_entry(upload_id, meta, parsed)] if parsed else [])
    assistant, retriever, critic = Assistant(ctx), Retriever(ctx), GroundingCritic(ctx)

    subs = assistant.plan(message, kind, cat)
    evidence: list[dict] = []
    if subs:
        try:
            evidence = retriever.retrieve(message, subs, cat)
        except AgentError as e:   # the writer is told nothing was found and must say so, not guess
            retriever.log("retrieve", status="error", output_summary=f"no usable data: {e.message}")
    masked, tokens = mask_rows(evidence)
    draft = assistant.write(message, kind, masked)
    rounds, history = 0, []
    while True:
        objections = critic.review(draft_text(kind, draft), masked, message, tokens, rounds + 1,
                                   recipients=draft.get("to") if kind == "email" else None)
        history.append(objections)
        if not objections or rounds >= MAX_ROUNDS:
            break
        rounds += 1
        draft = assistant.write(message, kind, masked, objections=objections, previous=draft)

    out = {"kind": kind, "model": ctx.model_name, "data_source": data.name, "personal_values_masked": len(tokens),
           "evidence": [{"question": e["question"], "sql": e["sql"], "rows": len(e["rows"]),
                         "truncated": bool(e.get("truncated"))} for e in evidence],
           "grounding": {"verified": not objections, "rounds": rounds, "objections": objections}}
    if kind == "question":
        out["answer"] = unmask(draft["answer"], tokens)
    else:
        out.update(to=[unmask(t, tokens) for t in draft["to"]], subject=unmask(draft["subject"], tokens),
                   body=unmask(draft["body"], tokens))
    tracer.event("Orchestrator", "assist_done", output_summary=f"{kind}: "
                 + ("grounded" if not objections else f"{len(objections)} unresolved objection(s)"))
    return out
