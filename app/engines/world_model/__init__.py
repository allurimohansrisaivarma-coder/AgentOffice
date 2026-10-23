"""World Model engine (Meterless): shared, queryable state with provenance.

    store    = WorldStoreService(path)            # canonical store + append-only log, read surface
    pipeline = IngestPipelineService(store)       # the only writer: 10-step idempotent ingest
    builder  = ContextBuilderService(store)       # structured, budgeted context for agents

Every agent reads its context from the World Model and writes what it learns
back through the pipeline. No model is called anywhere in this package.
"""

from app.engines.world_model.context import ContextBuilderService
from app.engines.world_model.ids import context_id, entity_id, normalize_name, relationship_id
from app.engines.world_model.ingest import (
    STEPS, IngestPipelineService, agent_source, entity_observation, relationship_observation, zetaris_source,
)
from app.engines.world_model.store import SCHEMA_VERSION, WorldStoreService


# Hours per week for one task come from several systems with different trust:
# the canonical value is the most confident source's, and disagreement stays visible as a conflict.
DEFAULT_POLICY = {"task": {"hours_per_week": "most_confident"}}


def open_world(settings) -> tuple[WorldStoreService, IngestPipelineService, ContextBuilderService]:
    store = WorldStoreService(settings.world_db_path)
    return store, IngestPipelineService(store, policy=DEFAULT_POLICY), ContextBuilderService(store)


__all__ = [
    "SCHEMA_VERSION", "STEPS", "ContextBuilderService", "IngestPipelineService", "WorldStoreService",
    "agent_source", "context_id", "entity_id", "entity_observation", "normalize_name", "open_world",
    "relationship_id", "relationship_observation", "zetaris_source",
]
