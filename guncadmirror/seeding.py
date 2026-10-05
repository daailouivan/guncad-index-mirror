from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event, Lock

from .cancellation import check_cancelled, wait_or_cancel
from .paths import ensure_within
from .qbittorrent import (
    QBitArtifactError,
    QBitClient,
    QBitConfigurationError,
    QBitError,
    QBitObservation,
    QBitRetryableError,
)
from .settings import Settings
from .state import JobStore, SeedingCandidate
from .torrent import TorrentError, parse_torrent
from .tracker_policy import TrackerPolicyManager

UNSAFE_DOWNLOAD_STATES = frozenset(
    {
        "allocating",
        "downloading",
        "error",
        "forcedDL",
        "metaDL",
        "missingFiles",
        "moving",
        "queuedDL",
        "stalledDL",
        "stoppedDL",
        "unknown",
    }
)


@dataclass(frozen=True, slots=True)
class SeedingCycleResult:
    considered: int = 0
    attempted: int = 0
    green: int = 0
    retrying: int = 0
    blocked: int = 0
    tracker_updates: int = 0
    tracker_errors: int = 0
    paused: bool = False
    error_code: str | None = None
    error: str | None = None

    def __add__(self, other: SeedingCycleResult) -> SeedingCycleResult:
        return SeedingCycleResult(
            considered=self.considered + other.considered,
            attempted=self.attempted + other.attempted,
            green=self.green + other.green,
            retrying=self.retrying + other.retrying,
            blocked=self.blocked + other.blocked,
            tracker_updates=self.tracker_updates + other.tracker_updates,
            tracker_errors=self.tracker_errors + other.tracker_errors,
            paused=self.paused or other.paused,
            error_code=other.error_code or self.error_code,
            error=other.error or self.error,
        )


@dataclass(frozen=True, slots=True)
class SeedPaths:
    payload_path: Path
    torrent_path: Path
    qbit_content_path: str
    qbit_save_path: str
    payload_size: int


@dataclass(frozen=True, slots=True)
class SeedReadiness:
    observation: QBitObservation
    tracker_updates: int = 0
    tracker_errors: int = 0


class SeedingScheduler:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        client: QBitClient,
        *,
        tracker_policy: TrackerPolicyManager | None = None,
        logger: logging.Logger | None = None,
        record_event: Callable[[str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.tracker_policy = tracker_policy
        self.logger = logger or logging.getLogger("guncad-mirror.seeding")
        self.record_event = record_event or (lambda _message: None)
        self.monotonic = monotonic
        self.sleep = sleep
        self._run_lock = Lock()
        self._tracker_reconciliation_degraded = False

    def close(self) -> None:
        self.client.close()

    def next_delay(self) -> float | None:
        if not self.settings.qbittorrent_enabled:
            return None
        return self.store.next_seeding_delay()

    def run(self, stop: Event | None = None) -> SeedingCycleResult:
        if not self.settings.qbittorrent_enabled:
            return SeedingCycleResult()
        with self._run_lock:
            candidates = self.store.seeding_candidates()
            result = SeedingCycleResult(considered=len(candidates))
            ready = [
                candidate
                for candidate in candidates
                if self.store.seeding_ready(candidate.job)
            ]
            if not ready or (stop is not None and stop.is_set()):
                return result
            try:
                client_version, _webapi_version = self.client.versions()
            except QBitConfigurationError as error:
                self.logger.error("qBittorrent seeding paused: %s", error.message)
                self.record_event(
                    f"qBittorrent seeding paused ({error.code}): {error.message}"
                )
                return SeedingCycleResult(
                    considered=len(candidates),
                    paused=True,
                    error_code=error.code,
                    error=error.message,
                )
            except QBitRetryableError as error:
                self.logger.warning("qBittorrent is unavailable: %s", error.message)
                self.record_event(
                    f"qBittorrent unavailable ({error.code}): {error.message}"
                )
                return SeedingCycleResult(
                    considered=len(candidates),
                    retrying=len(ready),
                    error_code=error.code,
                    error=error.message,
                )

            for candidate in ready:
                check_cancelled(stop)
                started = self.store.start_seeding(
                    candidate.release.id,
                    candidate.release.sd_hash,
                )
                if started is None:
                    continue
                result = _add_result(result, attempted=1)
                was_green = candidate.job.seeding_state.value == "green"
                try:
                    readiness = self._ensure_green(candidate, stop=stop)
                except QBitConfigurationError as error:
                    self._block(candidate, error)
                    self.logger.error("qBittorrent seeding paused: %s", error.message)
                    self.record_event(
                        f"qBittorrent seeding paused ({error.code}): {error.message}"
                    )
                    return _add_result(
                        result,
                        blocked=1,
                        paused=True,
                        error_code=error.code,
                        error=error.message,
                    )
                except QBitArtifactError as error:
                    self._block(candidate, error)
                    result = _add_result(result, blocked=1)
                    self.logger.error(
                        "Cannot seed %s: %s", candidate.release.name, error.message
                    )
                    self.record_event(
                        f"SEED BLOCKED for {self._label(candidate)} "
                        f"({error.code}): {error.message}"
                    )
                    continue
                except QBitRetryableError as error:
                    self._retry(candidate, error)
                    result = _add_result(result, retrying=1)
                    self.logger.warning(
                        "qBittorrent seeding will retry %s: %s",
                        candidate.release.name,
                        error.message,
                    )
                    event = "SEED LOST" if was_green else "Seed retry scheduled"
                    self.record_event(
                        f"{event} for {self._label(candidate)}: {error.message}"
                    )
                    continue
                except Exception as error:  # pragma: no cover - defensive boundary
                    wrapped = QBitRetryableError(
                        "seeder_error", f"{type(error).__name__}: {error}"
                    )
                    self._retry(candidate, wrapped)
                    result = _add_result(result, retrying=1)
                    self.logger.exception(
                        "Unexpected qBittorrent failure for %s", candidate.release.name
                    )
                    continue

                observation = readiness.observation
                self.store.mark_seed_green(
                    candidate.release.id,
                    candidate.release.sd_hash,
                    client_version=client_version,
                    observed_state=observation.torrent.state,
                    content_path=observation.torrent.content_path,
                    dht_nodes=observation.transfer.dht_nodes,
                    working_trackers=observation.working_trackers,
                    recheck_interval=self.settings.qbittorrent_recheck_interval,
                )
                result = _add_result(
                    result,
                    green=1,
                    tracker_updates=readiness.tracker_updates,
                    tracker_errors=readiness.tracker_errors,
                )
                if not was_green:
                    self.record_event(
                        f"SEED GREEN for {self._label(candidate)} "
                        f"({candidate.job.info_hash})"
                    )
            if (
                result.attempted
                and result.tracker_errors == 0
                and self._tracker_reconciliation_degraded
            ):
                self._tracker_reconciliation_degraded = False
                self.record_event("TRACKER RECONCILIATION RECOVERED")
            return result

    def _ensure_green(
        self,
        candidate: SeedingCandidate,
        *,
        stop: Event | None,
    ) -> SeedReadiness:
        paths = prepare_seed(self.settings, candidate)
        info_hash = candidate.job.info_hash
        if info_hash is None:  # pragma: no cover - store candidate invariant
            raise QBitArtifactError("missing_btih", "ledger has no torrent info hash")

        allowed_locations = self._seed_locations(candidate, paths)
        tracker_updates = 0
        tracker_errors = 0
        observation = self.client.observe(info_hash)
        activation_required = observation is None
        if observation is None:
            self.client.add(
                paths.torrent_path,
                save_path=paths.qbit_save_path,
                category=self.settings.qbittorrent_category,
                tag=self.settings.qbittorrent_tag,
            )
        else:
            content_path = PurePosixPath(observation.torrent.content_path)
            if (
                content_path not in allowed_locations
                and PurePosixPath(observation.torrent.save_path)
                != PurePosixPath(paths.qbit_save_path)
            ) or observation.torrent.state == "moving":
                self.logger.info(
                    "Re-adding qBittorrent seed %s from %s to %s",
                    candidate.release.name,
                    observation.torrent.save_path,
                    paths.qbit_save_path,
                )
                self.client.delete(info_hash, delete_files=False)
                self.client.add(
                    paths.torrent_path,
                    save_path=paths.qbit_save_path,
                    category=self.settings.qbittorrent_category,
                    tag=self.settings.qbittorrent_tag,
                )
                activation_required = True
            else:
                _validate_observation(observation, paths, allowed_locations)
                updated, failed = self._reconcile_trackers(candidate, observation)
                tracker_updates += updated
                tracker_errors += failed
                if observation.green and not updated:
                    return SeedReadiness(
                        observation,
                        tracker_updates=tracker_updates,
                        tracker_errors=tracker_errors,
                    )
                activation_required = not observation.green

        if activation_required:
            self.client.force_start(info_hash)
            self.client.reannounce(info_hash)
        deadline = self.monotonic() + self.settings.qbittorrent_ready_timeout
        last_detail = "torrent has not appeared in qBittorrent"
        while True:
            check_cancelled(stop)
            observation = self.client.observe(info_hash)
            if observation is not None:
                if observation.torrent.state == "moving":
                    last_detail = (
                        f"qBittorrent is moving torrent to {paths.qbit_save_path}"
                    )
                else:
                    _validate_observation(observation, paths, allowed_locations)
                    updated, failed = self._reconcile_trackers(candidate, observation)
                    tracker_updates += updated
                    tracker_errors += failed
                    if observation.green and not updated:
                        return SeedReadiness(
                            observation,
                            tracker_updates=tracker_updates,
                            tracker_errors=tracker_errors,
                        )
                    last_detail = _not_green_detail(observation)
            now = self.monotonic()
            if now >= deadline:
                raise QBitRetryableError(
                    "not_green",
                    f"qBittorrent did not become seed-ready: {last_detail}",
                )
            wait_or_cancel(
                stop,
                min(self.settings.qbittorrent_poll_interval, deadline - now),
                sleep=self.sleep,
            )

    def _reconcile_trackers(
        self,
        candidate: SeedingCandidate,
        observation: QBitObservation,
    ) -> tuple[int, int]:
        torrent = observation.torrent
        if (
            torrent.category != self.settings.qbittorrent_category
            or self.settings.qbittorrent_tag not in torrent.tags
        ):
            return 0, 0

        actual = tuple(
            dict.fromkeys(
                tracker.url
                for tracker in observation.trackers
                if tracker.is_network_tracker
            )
        )
        if self.tracker_policy is None:
            desired = self.settings.torrent_trackers
            removals_authoritative = True
        else:
            desired = self.tracker_policy.desired_trackers
            removals_authoritative = self.tracker_policy.removals_authoritative
        desired_set = set(desired)
        actual_set = set(actual)
        to_remove = (
            tuple(tracker for tracker in actual if tracker not in desired_set)
            if removals_authoritative
            else ()
        )
        to_add = tuple(tracker for tracker in desired if tracker not in actual_set)
        if not to_remove and not to_add:
            return 0, 0

        try:
            if to_add:
                self.client.add_trackers(torrent.info_hash, to_add)
            if to_remove:
                self.client.remove_trackers(torrent.info_hash, to_remove)
            self.client.reannounce(torrent.info_hash)
        except QBitError as error:
            self.logger.warning(
                "Tracker reconciliation will retry for %s (%s): %s",
                candidate.release.name,
                error.code,
                error.message,
            )
            if not self._tracker_reconciliation_degraded:
                self.record_event(
                    f"TRACKER RECONCILIATION DEGRADED ({error.code}): {error.message}"
                )
            self._tracker_reconciliation_degraded = True
            return 0, 1
        self.logger.info(
            "Reconciled qBittorrent trackers for %s: +%d -%d",
            candidate.release.name,
            len(to_add),
            len(to_remove),
        )
        return 1, 0

    def _seed_locations(
        self,
        candidate: SeedingCandidate,
        primary: SeedPaths,
    ) -> dict[PurePosixPath, PurePosixPath]:
        locations = {
            PurePosixPath(primary.qbit_content_path): PurePosixPath(
                primary.qbit_save_path
            )
        }
        info_hash = candidate.job.info_hash
        sha384 = candidate.job.sha384
        if info_hash is None or sha384 is None:  # pragma: no cover - store invariant
            return locations
        for alias in self.store.seeding_identity_candidates(info_hash, sha384):
            try:
                paths = prepare_seed(self.settings, alias)
            except QBitArtifactError:
                continue
            locations[PurePosixPath(paths.qbit_content_path)] = PurePosixPath(
                paths.qbit_save_path
            )
        return locations

    def _retry(self, candidate: SeedingCandidate, error: QBitRetryableError) -> None:
        self.store.retry_seeding(
            candidate.release.id,
            candidate.release.sd_hash,
            code=error.code,
            error=error.message,
            retry_backoff=self.settings.retry_backoff,
        )

    def _block(self, candidate: SeedingCandidate, error: QBitError) -> None:
        self.store.block_seeding(
            candidate.release.id,
            candidate.release.sd_hash,
            code=error.code,
            error=error.message,
            retry_after=self.settings.qbittorrent_recheck_interval,
        )

    @staticmethod
    def _label(candidate: SeedingCandidate) -> str:
        release = candidate.release
        return f"{release.channel_handle}/{release.name} [{release.sd_hash[:12]}]"


def prepare_seed(settings: Settings, candidate: SeedingCandidate) -> SeedPaths:
    job = candidate.job
    if job.file_path is None or job.torrent_path is None or job.info_hash is None:
        raise QBitArtifactError(
            "local_artifact_error", "ledger has no payload, torrent, or info hash"
        )
    try:
        payload_path = ensure_within(settings.data_dir, job.file_path)
        torrent_path = ensure_within(settings.outbox_dir, job.torrent_path)
    except ValueError as error:
        raise QBitArtifactError("local_artifact_error", str(error)) from error
    if not payload_path.is_file() or payload_path.stat().st_size <= 0:
        raise QBitArtifactError(
            "local_artifact_error", "verified payload is missing or empty"
        )
    if not torrent_path.is_file():
        raise QBitArtifactError("local_artifact_error", "torrent metainfo is missing")
    try:
        parsed = parse_torrent(torrent_path.read_bytes())
    except (OSError, TorrentError) as error:
        raise QBitArtifactError(
            "local_artifact_error", f"torrent cannot be parsed: {error}"
        ) from error
    payload_size = payload_path.stat().st_size
    if (
        parsed.info_hash != job.info_hash
        or parsed.file_name != payload_path.name
        or parsed.file_length != payload_size
    ):
        raise QBitArtifactError(
            "local_artifact_error", "torrent contradicts the durable ledger"
        )
    relative = payload_path.relative_to(settings.data_dir.resolve())
    qbit_content = PurePosixPath(settings.qbittorrent_data_dir.as_posix()).joinpath(
        *relative.parts
    )
    return SeedPaths(
        payload_path=payload_path,
        torrent_path=torrent_path,
        qbit_content_path=str(qbit_content),
        qbit_save_path=str(qbit_content.parent),
        payload_size=payload_size,
    )


def _validate_observation(
    observation: QBitObservation,
    paths: SeedPaths,
    allowed_locations: dict[PurePosixPath, PurePosixPath],
) -> None:
    torrent = observation.torrent
    content_path = PurePosixPath(torrent.content_path)
    expected_save_path = allowed_locations.get(content_path)
    if expected_save_path is None:
        raise QBitArtifactError(
            "content_path_conflict",
            "qBittorrent has the expected BTIH outside every verified path for "
            "that BTIH and SHA-384: "
            f"{torrent.content_path}",
        )
    if PurePosixPath(torrent.save_path) != expected_save_path:
        raise QBitArtifactError(
            "save_path_conflict",
            "qBittorrent has the expected BTIH at a different save path: "
            f"{torrent.save_path}",
        )
    if torrent.total_size != paths.payload_size:
        raise QBitArtifactError(
            "payload_size_conflict",
            "qBittorrent's torrent size contradicts the verified payload",
        )
    if torrent.state == "moving":
        raise QBitRetryableError(
            "torrent_moving",
            "qBittorrent is relocating torrent storage",
        )
    if torrent.state in UNSAFE_DOWNLOAD_STATES:
        raise QBitArtifactError(
            "torrent_not_complete",
            "qBittorrent is not treating the verified payload as complete: "
            f"state={torrent.state}",
        )


def _not_green_detail(observation: QBitObservation) -> str:
    torrent = observation.torrent
    transfer = observation.transfer
    return (
        f"state={torrent.state}, progress={torrent.progress:.3f}, "
        f"remaining={torrent.amount_left}, force_start={torrent.force_start}, "
        f"connection={transfer.connection_status}, dht_nodes={transfer.dht_nodes}, "
        f"working_trackers={observation.working_trackers}"
    )


def _add_result(
    result: SeedingCycleResult,
    *,
    attempted: int = 0,
    green: int = 0,
    retrying: int = 0,
    blocked: int = 0,
    tracker_updates: int = 0,
    tracker_errors: int = 0,
    paused: bool = False,
    error_code: str | None = None,
    error: str | None = None,
) -> SeedingCycleResult:
    return SeedingCycleResult(
        considered=result.considered,
        attempted=result.attempted + attempted,
        green=result.green + green,
        retrying=result.retrying + retrying,
        blocked=result.blocked + blocked,
        tracker_updates=result.tracker_updates + tracker_updates,
        tracker_errors=result.tracker_errors + tracker_errors,
        paused=result.paused or paused,
        error_code=error_code or result.error_code,
        error=error or result.error,
    )
