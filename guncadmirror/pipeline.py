from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event, Lock
from typing import Any

from .cancellation import AcquisitionCancelled
from .github import GitHubAcquirer
from .http_acquirer import HttpAcquirer
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
from .printables import PrintablesAcquirer
from .progress import (
    ActivityPhase,
    ActivityUpdate,
    NullProgressReporter,
    ProgressReporter,
)
from .publisher import Publisher
from .settings import Settings
from .state import Job, JobStore
from .torrent import TorrentError, create_torrent, parse_torrent
from .torrent_acquirer import TorrentAcquirer
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


@dataclass(slots=True)
class _DiskReservation:
    budget: _DiskBudget
    size: int
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.budget.release(self.size)
        self.released = True


class _DiskBudget:
    def __init__(self) -> None:
        self._reserved = 0
        self._lock = Lock()

    def acquire(
        self,
        size: int,
        *,
        data_dir: Path,
        reserve: int,
        disk_free: Callable[[Path], int],
    ) -> tuple[_DiskReservation | None, int]:
        with self._lock:
            available = disk_free(data_dir)
            required = reserve + self._reserved + size
            if available < required:
                return None, available
            self._reserved += size
        return _DiskReservation(self, size), available

    def release(self, size: int) -> None:
        with self._lock:
            self._reserved -= size
            if self._reserved < 0:  # pragma: no cover - internal invariant
                raise RuntimeError("disk reservation accounting underflow")


@dataclass(frozen=True, slots=True)
class _PreparedJob:
    release: Release
    directory: Path
    reservation: _DiskReservation


@dataclass(frozen=True, slots=True)
class _AcquiredJob:
    prepared: _PreparedJob
    file_path: Path
    evidence: AcquisitionEvidence


@dataclass(frozen=True, slots=True)
class _FallbackJob:
    prepared: _PreparedJob
    lbry_error: LbryError


@dataclass(frozen=True, slots=True)
class _DeferredJob:
    release: Release


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
        printables_acquirer: PrintablesAcquirer | None = None,
        github_acquirer: GitHubAcquirer | None = None,
        http_acquirer: HttpAcquirer | None = None,
        torrent_acquirer: TorrentAcquirer | None = None,
        disk_free: Callable[[Path], int] | None = None,
        logger: logging.Logger | None = None,
        progress: ProgressReporter | None = None,
        record_event: Callable[[str], None] | None = None,
    ):
        self.settings = settings
        self.index_client = index_client
        self.acquirer = acquirer
        self.store = store
        self.publisher = publisher
        self.fallback_acquirer = fallback_acquirer
        self.printables_acquirer = printables_acquirer or PrintablesAcquirer(
            progress=progress
        )
        self.github_acquirer = github_acquirer or GitHubAcquirer(progress=progress)
        self.http_acquirer = http_acquirer or HttpAcquirer(progress=progress)
        self.torrent_acquirer = torrent_acquirer or TorrentAcquirer(
            settings, progress=progress
        )
        self.disk_free = disk_free or (lambda path: shutil.disk_usage(path).free)
        self.logger = logger or logging.getLogger("guncad-mirror.pipeline")
        self.progress = progress or NullProgressReporter()
        self.record_event = record_event or (lambda _message: None)
        self._disk_budget = _DiskBudget()

    def run_cycle(self, stop: Event | None = None) -> CycleResult:
        result = CycleResult()
        try:
            releases = iter(self.index_client.releases(stop=stop))
        except AcquisitionCancelled:
            self.logger.info("Stopping Index cycle during page acquisition")
            return CycleResult(stopped=1)
        executors = {
            "lbry": ThreadPoolExecutor(
                max_workers=self.settings.lbry_concurrency,
                thread_name_prefix="mirror-lbry",
            ),
            "odysee": ThreadPoolExecutor(
                max_workers=self.settings.odysee_concurrency,
                thread_name_prefix="mirror-odysee",
            ),
            "printables": ThreadPoolExecutor(
                max_workers=self.settings.printables_concurrency,
                thread_name_prefix="mirror-printables",
            ),
            "github": ThreadPoolExecutor(
                max_workers=self.settings.github_concurrency,
                thread_name_prefix="mirror-github",
            ),
            "http": ThreadPoolExecutor(
                max_workers=self.settings.http_concurrency,
                thread_name_prefix="mirror-http",
            ),
            "torrent": ThreadPoolExecutor(
                max_workers=self.settings.torrent_concurrency,
                thread_name_prefix="mirror-torrent",
            ),
            "finalize": ThreadPoolExecutor(
                max_workers=self.settings.finalize_concurrency,
                thread_name_prefix="mirror-finalize",
            ),
        }
        futures: dict[Future[Any], tuple[str, _PreparedJob]] = {}
        seen: set[tuple[str, str]] = set()
        exhausted = False
        deferred: Release | None = None
        enumeration_error: Exception | None = None
        max_in_flight = (
            self.settings.lbry_concurrency
            + self.settings.odysee_concurrency
            + self.settings.printables_concurrency
            + self.settings.github_concurrency
            + self.settings.http_concurrency
            + self.settings.torrent_concurrency
            + self.settings.finalize_concurrency
        )
        try:
            while not exhausted or futures or deferred is not None:
                if stop is not None and stop.is_set():
                    exhausted = True
                    if deferred is not None:
                        result = result.add("stopped")
                        deferred = None
                    for future, (_stage, prepared) in tuple(futures.items()):
                        if future.cancel():
                            del futures[future]
                            result = result.add(self._finish_stopped(prepared))

                while not exhausted and len(futures) < max_in_flight:
                    if stop is not None and stop.is_set():
                        exhausted = True
                        break
                    if deferred is None:
                        try:
                            release = next(releases)
                        except StopIteration:
                            exhausted = True
                            break
                        except AcquisitionCancelled:
                            self.logger.info(
                                "Stopping Index cycle during page acquisition"
                            )
                            result = CycleResult(
                                discovered=result.discovered,
                                ready=result.ready,
                                skipped=result.skipped,
                                failed=result.failed,
                                stopped=result.stopped + 1,
                            )
                            exhausted = True
                            break
                        except Exception as error:
                            enumeration_error = error
                            exhausted = True
                            break

                        key = (release.id, release.sd_hash)
                        if key in seen:
                            self.logger.warning(
                                "Skipping duplicate Index release %s/%s",
                                release.id,
                                release.sd_hash,
                            )
                            result = result.add("skipped")
                            continue
                        seen.add(key)
                    else:
                        release = deferred
                    try:
                        prepared = self._prepare(release, stop=stop)
                    except Exception as error:
                        enumeration_error = error
                        exhausted = True
                        break
                    if isinstance(prepared, _DeferredJob):
                        deferred = release
                        break
                    deferred = None
                    if isinstance(prepared, str):
                        result = result.add(prepared)
                        if prepared == "stopped":
                            exhausted = True
                        continue
                    if prepared.release.platform == "printables":
                        stage = "printables"
                        operation = self._acquire_printables
                    elif prepared.release.platform == "github":
                        stage = "github"
                        operation = self._acquire_github
                    elif prepared.release.platform == "http":
                        stage = "http"
                        operation = self._acquire_http
                    elif prepared.release.platform == "torrent":
                        stage = "torrent"
                        operation = self._acquire_torrent
                    else:
                        stage = "lbry"
                        operation = self._acquire_lbry

                    future = executors[stage].submit(
                        operation,
                        prepared,
                        stop=stop,
                    )
                    futures[future] = (stage, prepared)

                if not futures:
                    continue
                completed, _pending = wait(
                    futures,
                    timeout=0.25,
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    stage, prepared = futures.pop(future)
                    if future.cancelled():
                        result = result.add(self._finish_stopped(prepared))
                        continue
                    try:
                        phase_result = future.result()
                    except AcquisitionCancelled:
                        result = result.add(self._finish_stopped(prepared))
                        continue
                    except Exception as error:
                        result = result.add(self._finish_failed(prepared, error))
                        continue

                    if stage == "finalize":
                        result = result.add(self._finish_ready(prepared, phase_result))
                        continue
                    if stop is not None and stop.is_set():
                        result = result.add(self._finish_stopped(prepared))
                        continue
                    if stage == "lbry" and isinstance(phase_result, _FallbackJob):
                        next_stage = "odysee"
                        operation = self._acquire_odysee
                    else:
                        next_stage = "finalize"
                        operation = self._finalize
                    next_future = executors[next_stage].submit(
                        operation,
                        phase_result,
                        stop=stop,
                    )
                    futures[next_future] = (next_stage, prepared)

            if enumeration_error is not None:
                raise enumeration_error
        finally:
            close = getattr(releases, "close", None)
            if close is not None:
                close()
            for executor in executors.values():
                executor.shutdown(wait=True, cancel_futures=True)
        return result

    def process(self, release: Release, *, stop: Event | None = None) -> str:
        prepared = self._prepare(release, stop=stop)
        if isinstance(prepared, str):
            return prepared
        if isinstance(prepared, _DeferredJob):
            return "skipped"
        try:
            if release.platform == "printables":
                acquired = self._acquire_printables(prepared, stop=stop)
            elif release.platform == "github":
                acquired = self._acquire_github(prepared, stop=stop)
            elif release.platform == "http":
                acquired = self._acquire_http(prepared, stop=stop)
            elif release.platform == "torrent":
                acquired = self._acquire_torrent(prepared, stop=stop)
            else:
                acquired = self._acquire_lbry(prepared, stop=stop)
                if isinstance(acquired, _FallbackJob):
                    acquired = self._acquire_odysee(acquired, stop=stop)
            bundle = self._finalize(acquired, stop=stop)
        except AcquisitionCancelled:
            return self._finish_stopped(prepared)
        except Exception as error:
            return self._finish_failed(prepared, error)
        return self._finish_ready(prepared, bundle)

    def _prepare(
        self,
        release: Release,
        *,
        stop: Event | None = None,
    ) -> _PreparedJob | _DeferredJob | str:
        if stop is not None and stop.is_set():
            return "stopped"
        exclusion_reason = self._policy_exclusion(release)
        if exclusion_reason is not None:
            self.logger.info("Skipping %s: %s", release.name, exclusion_reason)
            job = self.store.register(release)
            if job.state is not JobState.AWAITING_INDEX and (
                job.state is not JobState.EXCLUDED
                or job.exclusion_reason != exclusion_reason
            ):
                self.store.mark_excluded(release, exclusion_reason)
                self._record_event(
                    f"Excluded {self._release_label(release)}: {exclusion_reason}"
                )
            return "skipped"
        reservation, available_space = self._disk_budget.acquire(
            2 * (release.size or 0),
            data_dir=self.settings.data_dir,
            reserve=self.settings.min_free_space,
            disk_free=self.disk_free,
        )
        if reservation is None:
            release_requirement = self.settings.min_free_space + 2 * (release.size or 0)
            if available_space >= release_requirement:
                return _DeferredJob(release)
            message = (
                f"Storage pressure skipped {self._release_label(release)}: "
                f"{available_space} bytes free, {release_requirement} required"
            )
            self.logger.warning(
                "Skipping %s: %d bytes free, %d required for blobs, plaintext, and reserve",
                release.name,
                available_space,
                release_requirement,
            )
            self._record_event(message)
            return "skipped"

        directory = release_directory(
            self.settings.releases_dir,
            release.channel_handle,
            release.name,
            release.sd_hash,
        )
        prepared = _PreparedJob(release, directory, reservation)
        try:
            job = self.store.register(release)
        except Exception:
            reservation.release()
            raise
        if job.state is JobState.AWAITING_INDEX and self._ready_artifacts_exist(
            job, release
        ):
            self.logger.debug("Already prepared %s", release.name)
            reservation.release()
            return "skipped"
        if job.state is JobState.EXCLUDED and (
            job.exclusion_reason
            and "matches configured blacklist" not in job.exclusion_reason
            and "exceeds configured maximum" not in job.exclusion_reason
        ):
            self.logger.debug(
                "Manually excluded %s (%s)", release.name, job.exclusion_reason
            )
            reservation.release()
            return "skipped"

        if not self.store.ready_for_attempt(job) and job.state is JobState.FAILED:
            self.logger.debug("Backoff still active for %s", release.name)
            reservation.release()
            return "skipped"


        try:
            self.store.start_attempt(release)
            _atomic_json(directory / "release.json", release.raw)
        except Exception as error:
            return self._finish_failed(prepared, error)
        return prepared

    def _policy_exclusion(self, release: Release) -> str | None:
        if self._is_blacklisted(release):
            return f"channel {release.channel_handle} matches configured blacklist"
        if (
            self.settings.max_release_size
            and release.size is not None
            and release.size > self.settings.max_release_size
        ):
            return (
                f"{release.size} bytes exceeds configured maximum "
                f"{self.settings.max_release_size}"
            )
        return None

    def _acquire_lbry(
        self,
        prepared: _PreparedJob,
        *,
        stop: Event | None = None,
    ) -> _AcquiredJob | _FallbackJob:
        release = prepared.release
        try:
            file_path = self.acquirer.acquire(
                release,
                prepared.directory,
                stop=stop,
            )
        except LbryError as lbry_error:
            if (
                isinstance(lbry_error, LbryProtocolError)
                or self.fallback_acquirer is None
                or release.lbry_only
                or release.size is None
                or release.sha384 is None
            ):
                raise
            self.logger.warning(
                "LBRY acquisition failed for %s; queueing independently "
                "verifiable Odysee CDN fallback: %s",
                release.name,
                lbry_error,
            )
            self._record_event(
                f"LBRY unavailable; queued Odysee fallback for "
                f"{self._release_label(release)}: {self._error_text(lbry_error)}"
            )
            return _FallbackJob(prepared, lbry_error)
        return _AcquiredJob(
            prepared,
            file_path,
            AcquisitionEvidence(AcquisitionTransport.LBRY),
        )

    def _acquire_odysee(
        self,
        fallback_job: _FallbackJob,
        *,
        stop: Event | None = None,
    ) -> _AcquiredJob:
        if self.fallback_acquirer is None:  # pragma: no cover - guarded upstream
            raise RuntimeError("Odysee fallback is not configured")
        release = fallback_job.prepared.release
        try:
            fallback = self.fallback_acquirer.acquire(
                release,
                fallback_job.prepared.directory,
                stop=stop,
            )
        except AcquisitionCancelled:
            raise
        except Exception as fallback_error:
            lbry_error = fallback_job.lbry_error
            raise RuntimeError(
                f"LBRY acquisition failed ({type(lbry_error).__name__}: "
                f"{lbry_error}); Odysee fallback also failed "
                f"({type(fallback_error).__name__}: {fallback_error})"
            ) from fallback_error
        return _AcquiredJob(
            fallback_job.prepared,
            fallback.path,
            AcquisitionEvidence(
                AcquisitionTransport.ODYSEE_CDN,
                source_url=fallback.source_url,
                lbry_failure=(
                    f"{type(fallback_job.lbry_error).__name__}: "
                    f"{fallback_job.lbry_error}"
                ),
            ),
        )

    def _acquire_printables(
        self,
        prepared: _PreparedJob,
        *,
        stop: Event | None = None,
    ) -> _AcquiredJob:
        acquisition = self.printables_acquirer.acquire(
            prepared.release,
            prepared.directory,
            stop=stop,
        )
        return _AcquiredJob(
            prepared,
            acquisition.path,
            AcquisitionEvidence(
                AcquisitionTransport.PRINTABLES,
                source_url=acquisition.source_url,
            ),
        )

    def _acquire_github(
        self,
        prepared: _PreparedJob,
        *,
        stop: Event | None = None,
    ) -> _AcquiredJob:
        acquisition = self.github_acquirer.acquire(
            prepared.release,
            prepared.directory,
            stop=stop,
        )
        return _AcquiredJob(
            prepared,
            acquisition.path,
            AcquisitionEvidence(
                AcquisitionTransport.GITHUB,
                source_url=acquisition.source_url,
            ),
        )

    def _acquire_http(
        self,
        prepared: _PreparedJob,
        *,
        stop: Event | None = None,
    ) -> _AcquiredJob:
        acquisition = self.http_acquirer.acquire(
            prepared.release,
            prepared.directory,
            stop=stop,
        )
        return _AcquiredJob(
            prepared,
            acquisition.path,
            AcquisitionEvidence(
                AcquisitionTransport.HTTP,
                source_url=acquisition.source_url,
            ),
        )

    def _acquire_torrent(
        self,
        prepared: _PreparedJob,
        *,
        stop: Event | None = None,
    ) -> _AcquiredJob:
        acquisition = self.torrent_acquirer.acquire(
            prepared.release,
            prepared.directory,
            stop=stop,
        )
        return _AcquiredJob(
            prepared,
            acquisition.path,
            AcquisitionEvidence(
                AcquisitionTransport.TORRENT,
                source_url=acquisition.source_url,
            ),
        )

    def _finalize(
        self,
        acquired: _AcquiredJob,
        *,
        stop: Event | None = None,
    ) -> PublicationBundle:
        release = acquired.prepared.release
        try:
            total_bytes = acquired.file_path.stat().st_size
            hashes = verify_file(
                release,
                acquired.file_path,
                stop=stop,
                progress=self._byte_progress(
                    release,
                    ActivityPhase.VERIFY,
                    total_bytes,
                ),
            )
        except VerificationError:
            if acquired.evidence.transport is AcquisitionTransport.ODYSEE_CDN:
                acquired.file_path.unlink(missing_ok=True)
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
            file_path=acquired.file_path,
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
            acquired.file_path,
            torrent_path,
            piece_length=self.settings.torrent_piece_length,
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
        bundle = self.publisher.publish(release, hashes, torrent, acquired.evidence)
        self._validate_bundle(bundle)
        self.store.mark_awaiting_index(release, torrent)
        return bundle

    def _finish_ready(
        self,
        prepared: _PreparedJob,
        bundle: PublicationBundle,
    ) -> str:
        self.logger.info(
            "Prepared %s for Index publication: %s",
            prepared.release.name,
            bundle.manifest_path,
        )
        self._release_resources(prepared)
        return "ready"

    def _finish_stopped(self, prepared: _PreparedJob) -> str:
        release = prepared.release
        self.logger.info(
            "Paused %s with resumable acquisition state intact", release.name
        )
        self._record_event(
            f"Paused {self._release_label(release)}; resumable state preserved"
        )
        self._release_resources(prepared)
        return "stopped"

    def _finish_failed(self, prepared: _PreparedJob, error: Exception) -> str:
        release = prepared.release
        try:
            self.store.mark_failed(
                release,
                error,
                retry_backoff=self.settings.retry_backoff,
            )
            self._record_event(
                f"FAILED {self._release_label(release)}: {self._error_text(error)}"
            )
            self.logger.error(
                "Failed to prepare %s",
                release.name,
                exc_info=(type(error), error, error.__traceback__),
            )
        finally:
            self._release_resources(prepared)
        return "failed"

    def _release_resources(self, prepared: _PreparedJob) -> None:
        prepared.reservation.release()
        self.progress.clear_activity(prepared.release)

    def _record_event(self, message: str) -> None:
        try:
            self.record_event(message)
        except Exception:
            self.logger.exception("Failed to record operator event")

    @staticmethod
    def _release_label(release: Release) -> str:
        return f"{release.channel_handle}/{release.name} [{release.sd_hash[:12]}]"

    @staticmethod
    def _error_text(error: Exception) -> str:
        text = f"{type(error).__name__}: {error}"
        return text if len(text) <= 500 else f"{text[:497]}..."

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
            parsed_torrent = parse_torrent(safe_torrent.read_bytes())
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
                and torrent_document.get("file_name")
                in {safe_file.name, safe_torrent.name}
                and torrent_document.get("piece_length")
                == self.settings.torrent_piece_length
                and torrent_document.get("piece_count")
                == _piece_count(actual_size, self.settings.torrent_piece_length)
                and torrent_document.get("magnet_uri") == magnet_uri
                and torrent_document.get("trackers") == list(parsed_torrent.trackers)
                and torrent_document.get("sha256") == _sha256_file(safe_torrent)
                and parsed_torrent.info_hash == info_hash
                and parsed_torrent.magnet_uri == magnet_uri
                and parsed_torrent.file_name == safe_file.name
                and parsed_torrent.file_length == actual_size
                and _valid_acquisition_document(acquisition_document)
            )
        except (
            OSError,
            RuntimeError,
            TorrentError,
            UnicodeError,
            ValueError,
            TypeError,
        ):
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
