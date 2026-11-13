"""Connection-owned native colony vitals cache."""

from __future__ import annotations

from .native_snapshot import NativeSnapshotProvider

VITALS_VERSION = "colony_vitals.v1"


class VitalsProvider(NativeSnapshotProvider):
    operation = "read_colony_vitals"
    version = VITALS_VERSION
    # Food and wounds change within half a day; same as attention urgent_review.
    refresh_ticks = 2_500
    label = "Colony vitals"
