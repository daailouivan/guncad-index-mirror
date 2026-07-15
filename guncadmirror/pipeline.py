from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event

from .cancellation import AcquisitionCancelled
from .index_client import IndexClient
from .lbry import LbryAcquirer, LbryError, LbryProtocolError
from .models import (
    AcquisitionEvidence,
    AcquisitionTransport,
    JobState,
    PublicationBundle,
    Release,
)
from .odysee import OdyseeAcquirer
from .paths import ensure_within, release_directory
from .progress import (
    ActivityPhase,
    ActivityUpdate,
    NullProgressReporter,
    ProgressReporter,
)
from .publisher import Publisher
from .settings import Settings
from .state import Job, JobStore
from .torrent import create_torrent
from .verification import VerificationError, verify_file


@dataclass(frozen=True, slots=True)
class CycleResult:
    discovered: int = 0
    ready: int = 0
    skipped: int = 0
    failed: int = 0
    stopped: int = 0

    def add(self, outcome: str) -> CycleResult:
        values = {
            "discovered": self.discovered + 1,
            "ready": self.ready + (outcome == "ready"),
            "skipped": self.skipped + (outcome == "skipped"),
            "failed": self.failed + (outcome == "failed"),
            "stopped": self.stopped + (outcome == "stopped"),
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
        fallback_acquirer: OdyseeAcquirer | None = None,
        disk_free: Callable[[Path], int] | None = None,
        logger: logging.Logger | None = None,
        progress: ProgressReporter | None = None,
    ):
        self.settings = settings
        self.index_client = index_client
        self.acquirer = acquirer
        self.store = store
        self.publisher = publisher
        self.fallback_acquirer = fallback_acquirer
        self.disk_free = disk_free or (lambda path: shutil.disk_usage(path).free)
        self.logger = logger or logging.getLogger("guncad-mirror.pipeline")
        self.progress = progress or NullProgressReporter()

    def run_cycle(self, stop: Event | None = None) -> CycleResult:
        result = CycleResult()
        try:
            for release in self.index_client.releases(stop=stop):
                if stop is not None and stop.is_set():
                    self.logger.info("Stopping Index cycle at a release boundary")
                    break
                outcome = self.process(release, stop=stop)
                result = result.add(outcome)
                if outcome == "stopped":
                    break
        except AcquisitionCancelled:
            self.logger.info("Stopping Index cycle during page acquisition")
            result = CycleResult(
                discovered=result.discovered,
                ready=result.ready,
                skipped=result.skipped,
                failed=result.failed,
                stopped=result.stopped + 1,
            )
        return result

    def process(self, release: Release, *, stop: Event | None = None) -> str:
        if stop is not None and stop.is_set():
            return "stopped"
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
        if job.state is JobState.AWAITING_INDEX and self._ready_artifacts_exist(
            job, release
        ):
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
            acquisition = AcquisitionEvidence(AcquisitionTransport.LBRY)
            try:
                file_path = self.acquirer.acquire(release, directory, stop=stop)
            except LbryError as lbry_error:
                if (
                    isinstance(lbry_error, LbryProtocolError)
                    or self.fallback_acquirer is None
                    or release.size is None
                    or release.sha384 is None
                ):
                    raise
                self.logger.warning(
                    "LBRY acquisition failed for %s; trying independently "
                    "verifiable Odysee CDN fallback: %s",
                    release.name,
                    lbry_error,
                )
                try:
                    fallback = self.fallback_acquirer.acquire(
                        release, directory, stop=stop
                    )
                except AcquisitionCancelled:
                    raise
                except Exception as fallback_error:
                    raise RuntimeError(
                        f"LBRY acquisition failed ({type(lbry_error).__name__}: "
                        f"{lbry_error}); Odysee fallback also failed "
                        f"({type(fallback_error).__name__}: {fallback_error})"
                    ) from fallback_error
                file_path = fallback.path
                acquisition = AcquisitionEvidence(
                    AcquisitionTransport.ODYSEE_CDN,
                    source_url=fallback.source_url,
                    lbry_failure=f"{type(lbry_error).__name__}: {lbry_error}",
                )
            try:
                total_bytes = file_path.stat().st_size
                hashes = verify_file(
                    release,
                    file_path,
                    stop=stop,
                    progress=self._byte_progress(
                        release,
                        ActivityPhase.VERIFY,
                        total_bytes,
                    ),
                )
            except VerificationError:
                if acquisition.transport is AcquisitionTransport.ODYSEE_CDN:
                    file_path.unlink(missing_ok=True)
                raise
            if (
                self.settings.max_release_size
                and hashes.size > self.settings.max_release_size
            ):
                raise ValueError(
                    f"assembled payload has {hashes.size} bytes, exceeding configured "
                    f"maximum {self.settings.max_release_size}"
                )
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
                stop=stop,
                progress=self._byte_progress(
                    release,
                    ActivityPhase.TORRENT,
                    hashes.size,
                ),
            )
            self.progress.update_activity(
                ActivityUpdate(release=release, phase=ActivityPhase.OUTBOX)
            )
            bundle = self.publisher.publish(release, hashes, torrent, acquisition)
            self._validate_bundle(bundle)
            self.store.mark_awaiting_index(release, torrent)
        except AcquisitionCancelled:
            self.logger.info(
                "Paused %s with resumable acquisition state intact", release.name
            )
            return "stopped"
        except Exception as error:
            self.store.mark_failed(
                release,
                error,
                retry_backoff=self.settings.retry_backoff,
            )
            self.logger.exception("Failed to prepare %s", release.name)
            return "failed"
        finally:
            self.progress.clear_activity()

        self.logger.info(
            "Prepared %s for Index publication: %s",
            release.name,
            bundle.manifest_path,
        )
        return "ready"

    def _byte_progress(
        self,
        release: Release,
        phase: ActivityPhase,
        total_bytes: int,
    ) -> Callable[[int], None]:
        def report(completed_bytes: int) -> None:
            self.progress.update_activity(
                ActivityUpdate(
                    release=release,
                    phase=phase,
                    completed_bytes=completed_bytes,
                    total_bytes=total_bytes,
                )
            )

        return report

    def _is_blacklisted(self, release: Release) -> bool:
        normalized = release.channel_handle.removeprefix("@").replace(":", "#")
        return any(
            normalized.startswith(pattern)
            for pattern in self.settings.blacklisted_handles
        )

    def _ready_artifacts_exist(self, job: Job, release: Release) -> bool:
        file_path = job.file_path
        torrent_path = job.torrent_path
        sha384 = job.sha384
        sha256 = job.sha256
        info_hash = job.info_hash
        magnet_uri = job.magnet_uri
        if (
            not file_path
            or not torrent_path
            or not sha384
            or (release.sha384 is not None and sha384 != release.sha384)
            or not sha256
            or not info_hash
            or not magnet_uri
        ):
            return False
        manifest = (
            self.settings.outbox_dir / job.release_id / job.sd_hash / "manifest.json"
        )
        try:
            safe_file = ensure_within(self.settings.data_dir, Path(file_path))
            safe_torrent = ensure_within(self.settings.outbox_dir, Path(torrent_path))
            actual_size = safe_file.stat().st_size if safe_file.is_file() else 0
            if (
                not safe_file.is_file()
                or actual_size == 0
                or (release.size is not None and actual_size != release.size)
                or not safe_torrent.is_file()
                or safe_torrent.stat().st_size == 0
            ):
                return False
            document = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                return False
            release_document = document.get("release")
            lbry_document = document.get("lbry")
            artifact_document = document.get("artifact")
            torrent_document = document.get("torrent")
            acquisition_document = document.get("acquisition")
            if not all(
                isinstance(value, dict)
                for value in (
                    release_document,
                    lbry_document,
                    artifact_document,
                    torrent_document,
                )
            ):
                return False
            return (
                document.get("schema") == "guncad-mirror-publication-v1"
                and document.get("status") == "awaiting-index"
                and release_document.get("id") == release.id
                and release_document.get("name") == release.name
                and release_document.get("channel_handle") == release.channel_handle
                and release_document.get("url") == release.url
                and release_document.get("url_lbry") == release.url_lbry
                and lbry_document.get("sd_hash") == release.sd_hash
                and lbry_document.get("claimed_sha384") == release.sha384
                and artifact_document.get("size") == actual_size
                and artifact_document.get("file_name") == safe_file.name
                and artifact_document.get("sha384") == sha384
                and artifact_document.get("sha256") == sha256
                and torrent_document.get("btih") == info_hash
                and torrent_document.get("file_name") == safe_torrent.name
                and torrent_document.get("piece_length")
                == self.settings.torrent_piece_length
                and torrent_document.get("piece_count")
                == _piece_count(actual_size, self.settings.torrent_piece_length)
                and torrent_document.get("magnet_uri") == magnet_uri
                and torrent_document.get("trackers")
                == list(self.settings.torrent_trackers)
                and torrent_document.get("sha256") == _sha256_file(safe_torrent)
                and _valid_acquisition_document(acquisition_document)
            )
        except (OSError, RuntimeError, UnicodeError, ValueError, TypeError):
            return False

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


def _piece_count(size: int, piece_length: int) -> int:
    return (size + piece_length - 1) // piece_length


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_acquisition_document(value: object) -> bool:
    # Manifests generated before fallback support necessarily came from LBRY.
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    transport = value.get("transport")
    source_url = value.get("source_url")
    lbry_failure = value.get("lbry_failure")
    if transport == AcquisitionTransport.LBRY:
        return source_url is None and lbry_failure is None
    if transport != AcquisitionTransport.ODYSEE_CDN:
        return False
    return (
        isinstance(source_url, str)
        and source_url.startswith("https://player.odycdn.com/")
        and isinstance(lbry_failure, str)
        and bool(lbry_failure)
    )
