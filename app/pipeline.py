"""The three-stage AgentOffice pipeline (Discover -> Explain -> Decide & Build).

Not built yet: build order step 4 (docs/PROJECT_PLAN.md section 7) replaces
this with the PM -> Engineer -> QA -> Manager loop. Until then /run reports a
structured `not_implemented` error rather than returning anything made up.
"""

from __future__ import annotations

from typing import Optional

from app.config import Settings
from app.llm import Generator
from app.tracing import Tracer


def run_pipeline(user_query: str, context: dict, *, settings: Settings, tracer: Tracer,
                 generator: Optional[Generator]) -> dict:
    raise NotImplementedError("the agent pipeline is not built yet (build order step 4)")
