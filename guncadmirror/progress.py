from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .models import AcquisitionTransport, Release


class ActivityPhase(StrEnum):
    LBRY = "Acquiring from LBRY"
    ODYSEE = "Acquiring from Odysee CDN"
    PRINTABLES = "Acquiring from Printables"
    GITHUB = "Acquiring from GitHub"
    HTTP = "Acquiring via HTTP"
    TORRENT_SWARM = "Acquiring from BitTorrent"
    VERIFY = "Verifying plaintext"
    TORRENT = "Hashing BitTorrent pieces"
    OUTBOX = "Writing local outbox"


@dataclass(frozen=True, slots=True)
class ActivityUpdate:
    release: Release
    phase: ActivityPhase
    transport: AcquisitionTransport | None = None
    completed_bytes: int | None = None
    total_bytes: int | None = None
    blobs_remaining: int | None = None


class ProgressReporter(Protocol):
    def update_activity(self, update: ActivityUpdate) -> None: ...

    def clear_activity(self, release: Release | None = None) -> None: ...


class NullProgressReporter:
    def update_activity(self, update: ActivityUpdate) -> None:
        pass

    def clear_activity(self, release: Release | None = None) -> None:
        pass
