"""H-MEM engine (Meterless): persistent, tiered, conflict-resolved memory across runs.

    store     = MemoryStoreService(path, TrustLedgerService(ledger_path))   # tiers, approval boundary, conflicts
    retrieval = MemoryRetrievalService(store)                               # 8-signal hybrid ranking + trace
    mining    = app.engines.hmem.mining                                     # events -> proposals (direct capture fallback)

Not implemented from the reference: dreaming and sleep consolidation (the
hackathon brief's minimum is store + retrieval).
"""

from pathlib import Path

from app.engines.hmem.ledger import TrustLedgerService
from app.engines.hmem.retrieval import MemoryRetrievalService
from app.engines.hmem.store import ApprovalPolicy, ConflictDetectionService, MemoryStoreService, new_record


def open_memory(settings, sample_mode: bool) -> tuple[MemoryStoreService, MemoryRetrievalService]:
    suffix = "-sample" if sample_mode else ""
    ledger = TrustLedgerService(Path(settings.log_dir) / f"hmem-ledger{suffix}.jsonl")
    store = MemoryStoreService(Path(settings.world_db_path).parent / f"memory{suffix}.db", ledger)
    return store, MemoryRetrievalService(store)


__all__ = ["ApprovalPolicy", "ConflictDetectionService", "MemoryRetrievalService", "MemoryStoreService",
           "TrustLedgerService", "new_record", "open_memory"]
