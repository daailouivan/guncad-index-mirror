from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from .index_client import IndexClient
from .lbry import LbryAcquirer
from .models import JobState, PublicationBundle, Release
from .paths import release_directory
from .publisher import Publisher
from .settings import Settings
from .state import Job, JobStore
from .torrent import create_torrent
from .verification import verify_file


@dataclass(frozen=True, slots=True)
class CycleResult:
    discovered: int = 0
    ready: int = 0
    skipped: int = 0
    failed: int = 0

    def add(self, outcome: str) -> CycleResult:
        values = {
            "discovered": self.discovered + 1,
            "ready": self.ready + (outcome == "ready"),
            "skipped": self.skipped + (outcome == "skipped"),
            "failed": self.failed + (outcome == "failed"),
        }
        return CycleResult(**values)


class MirrorPipeline:
    def __init__(
        self,
        settings: Settings,
        index_client: IndexClient,
        acquirer: LbryAcquirer,
        store: JobStore,
        publisher: Publisher,
        *,
        disk_free: Callable[[Path], int] | None = None,
        logger: logging.Logger | None = None,
    ):
        self.settings = settings
        self.index_client = index_client
        self.acquirer = acquirer
        self.store = store
        self.publisher = publisher
        self.disk_free = disk_free or (lambda path: shutil.disk_usage(path).free)
        self.logger = logger or logging.getLogger("guncad-mirror.pipeline")

    def run_cycle(self) -> CycleResult:
        result = CycleResult()
        for release in self.index_client.releases():
            outcome = self.process(release)
            result = result.add(outcome)
        return result

    def process(self, release: Release) -> str:
        if self._is_blacklisted(release):
            self.logger.info("Skipping blacklisted channel %s", release.channel_handle)
            return "skipped"
        if (
            self.settings.max_release_size
            and release.size is not None
            and release.size > self.settings.max_release_size
        ):
            self.logger.info(
                "Skipping %s: %d bytes exceeds configured maximum",
                release.name,
                release.size,
            )
            return "skipped"
        required_space = self.settings.min_free_space + 2 * (release.size or 0)
        available_space = self.disk_free(self.settings.data_dir)
        if available_space < required_space:
            self.logger.warning(
                "Skipping %s: %d bytes free, %d required for blobs, plaintext, and reserve",
                release.name,
                available_space,
                required_space,
            )
            return "skipped"

        job = self.store.register(release)
        if job.state is JobState.AWAITING_INDEX and self._ready_artifacts_exist(job):
            self.logger.debug("Already prepared %s", release.name)
            return "skipped"
        if not self.store.ready_for_attempt(job) and job.state is JobState.FAILED:
            self.logger.debug("Backoff still active for %s", release.name)
            return "skipped"

        self.store.start_attempt(release)
        directory = release_directory(
            self.settings.releases_dir,
            release.channel_handle,
            release.name,
            release.sd_hash,
        )
        try:
            _atomic_json(directory / "release.json", release.raw)
            file_path = self.acquirer.acquire(release, directory)
            hashes = verify_file(release, file_path)
            self.store.mark_verified(
                release,
                file_path=file_path,
                sha384=hashes.sha384,
                sha256=hashes.sha256,
            )
            torrent_path = (
                self.settings.outbox_dir
                / release.id
                / release.sd_hash
                / f"{hashes.sha384}.torrent"
            )
            torrent = create_torrent(
                file_path,
                torrent_path,
                piece_length=self.settings.torrent_piece_length,
                trackers=self.settings.torrent_trackers,
            )
            bundle = self.publisher.publish(release, hashes, torrent)
            self._validate_bundle(bundle)
            self.store.mark_awaiting_index(release, torrent)
        except Exception as error:
            self.store.mark_failed(
                release,
                error,
                retry_backoff=self.settings.retry_backoff,
            )
            self.logger.exception("Failed to prepare %s", release.name)
            return "failed"

        self.logger.info(
            "Prepared %s for Index publication: %s",
            release.name,
            bundle.manifest_path,
        )
        return "ready"

    def _is_blacklisted(self, release: Release) -> bool:
        normalized = release.channel_handle.removeprefix("@").replace(":", "#")
        return any(
            normalized.startswith(pattern)
            for pattern in self.settings.blacklisted_handles
        )

    def _ready_artifacts_exist(self, job: Job) -> bool:
        file_path = job.file_path
        torrent_path = job.torrent_path
        sha384 = job.sha384
        if not file_path or not torrent_path or not sha384:
            return False
        manifest = (
            self.settings.outbox_dir / job.release_id / job.sd_hash / "manifest.json"
        )
        return (
            Path(file_path).is_file()
            and Path(torrent_path).is_file()
            and manifest.is_file()
        )

    @staticmethod
    def _validate_bundle(bundle: PublicationBundle) -> None:
        if (
            not bundle.torrent.torrent_path.is_file()
            or not bundle.manifest_path.is_file()
        ):
            raise RuntimeError("publisher returned before durable outbox files existed")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temp:
        temporary_path = Path(temp.name)
        temp.write(content)
        temp.flush()
        os.fsync(temp.fileno())
    temporary_path.replace(path)
