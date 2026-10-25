"""Long-lived state shared by the API and background jobs: stores, ledgers and engines, one set per mode."""

from __future__ import annotations

import threading
from dataclasses import replace

from app.approvals import ApprovalLedger
from app.config import Settings
from app.engines.hmem import open_memory
from app.engines.world_model import open_world
from app.jobs import JobManager
from app.tasks.files import UploadStore
from app.tasks.pipeline import TaskStore


class Services:
    def __init__(self, settings: Settings):
        self.settings = settings
        state = settings.world_db_path.parent
        self.approvals = ApprovalLedger(state)
        self.uploads = UploadStore(state)
        self.tasks = TaskStore(state)
        self.jobs = JobManager()
        self._lock = threading.Lock()
        self._worlds: dict[bool, tuple] = {}
        self._memories: dict[bool, tuple] = {}

    def world(self, sample_mode: bool) -> tuple:
        """One shared World Model per mode, so sample runs never mix with real Zetaris facts."""
        with self._lock:
            if sample_mode not in self._worlds:
                path = self.settings.world_db_path
                self._worlds[sample_mode] = open_world(replace(self.settings, world_db_path=path.with_name(
                    path.stem + "-sample" + path.suffix)) if sample_mode else self.settings)
            return self._worlds[sample_mode]

    def memory(self, sample_mode: bool) -> tuple:
        """One H-MEM store per mode, kept apart like the World Models."""
        with self._lock:
            if sample_mode not in self._memories:
                self._memories[sample_mode] = open_memory(self.settings, sample_mode)
            return self._memories[sample_mode]
