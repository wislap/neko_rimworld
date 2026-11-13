"""Connection-owned native situation cache."""

from __future__ import annotations

from .native_snapshot import NativeSnapshotProvider

SITUATION_VERSION = "situation_snapshot.v1"


class SituationProvider(NativeSnapshotProvider):
    operation = "read_situation"
    version = SITUATION_VERSION
    refresh_ticks = 30_000
    label = "Situation"
