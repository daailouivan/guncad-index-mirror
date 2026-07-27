from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Event, Lock
from typing import Any

from .index_publisher import (
    BTIH_RE,
    MAX_MANIFEST_BYTES,
    MAX_TORRENT_BYTES,
    REQUEST_SCHEMA,
    SHA256_RE,
    SHA384_RE,
    IndexPublisherClient,
    PublicationPaused,
    PublicationResult,
    PublicationSubmission,
    RetryablePublicationError,
    encode_manifest,
)
from .models import AcquisitionTransport, PublicationState
from .paths import ensure_within
from .settings import Settings
from .state import JobStore, PublicationCandidate
from .torrent import TorrentError, parse_torrent, strip_torrent_trackers


class PublicationPreparationError(RuntimeError):
    """A staged artifact cannot currently be published safely."""


@dataclass(frozen=True, slots=True)
class PublicationCycleResult:
    considered: int = 0
    attempted: int = 0
    published: int = 0
    duplicates: int = 0
    rejected: int = 0
    conflicts: int = 0
    retrying: int = 0
    paused: bool = False

    def __add__(self, other: PublicationCycleResult) -> PublicationCycleResult:
        return PublicationCycleResult(
            considered=self.considered + other.considered,
            attempted=self.attempted + other.attempted,
            published=self.published + other.published,
            duplicates=self.duplicates + other.duplicates,
            rejected=self.rejected + other.rejected,
            conflicts=self.conflicts + other.conflicts,
            retrying=self.retrying + other.retrying,
            paused=self.paused or other.paused,
        )


class PublicationScheduler:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        client: IndexPublisherClient,
        *,
        logger: logging.Logger | None = None,
        record_event: Callable[[str], None] | None = None,
    ):
        self.settings = settings
        self.store = store
        self.client = client
        self.logger = logger or logging.getLogger("guncad-mirror.publication")
        self.record_event = record_event or (lambda _message: None)
        self._run_lock = Lock()

    def close(self) -> None:
        self.client.close()

    def next_delay(self) -> float | None:
        if not self.settings.publish_enabled:
            return None
        leaders: dict[str, PublicationCandidate] = {}
        for candidate in self.store.publication_candidates():
            sha384 = candidate.job.sha384
            if sha384 is None:
                continue
            incumbent = leaders.get(sha384)
            if incumbent is None or _candidate_order(candidate) < _candidate_order(
                incumbent
            ):
                leaders[sha384] = candidate
        if not leaders:
            return None
        return min(
            max(
                candidate.job.publication_next_attempt_at - self.store.clock(),
                0,
            )
            for candidate in leaders.values()
        )

    def run(self, stop: Event | None = None) -> PublicationCycleResult:
        if not self.settings.publish_enabled:
            return PublicationCycleResult()
        with self._run_lock:
            candidates = self.store.publication_candidates()
            result = PublicationCycleResult(considered=len(candidates))
            if not candidates or (stop is not None and stop.is_set()):
                return result

            sha_groups: dict[str, list[PublicationCandidate]] = defaultdict(list)
            for candidate in candidates:
                if candidate.job.sha384 is not None:
                    sha_groups[candidate.job.sha384].append(candidate)

            pause = Event()
            with ThreadPoolExecutor(
                max_workers=self.settings.publish_concurrency,
                thread_name_prefix="mirror-publish",
            ) as executor:
                futures = [
                    executor.submit(self._process_sha_group, group, pause, stop)
                    for group in sha_groups.values()
                ]
                for future in as_completed(futures):
                    result += future.result()
            return result

    def _process_sha_group(
        self,
        candidates: Sequence[PublicationCandidate],
        pause: Event,
        stop: Event | None,
    ) -> PublicationCycleResult:
        result = PublicationCycleResult()
        descriptor_groups: dict[tuple[str, str | None], list[PublicationCandidate]] = (
            defaultdict(list)
        )
        for candidate in candidates:
            descriptor_groups[
                (candidate.release.sd_hash, candidate.job.info_hash)
            ].append(candidate)
        ordered = sorted(
            descriptor_groups.values(),
            key=lambda group: _candidate_order(min(group, key=_candidate_order)),
        )

        for aliases in ordered:
            aliases.sort(key=_candidate_order)
            if pause.is_set() or (stop is not None and stop.is_set()):
                return result
            descriptor_finished = False
            for alias_index, candidate in enumerate(aliases):
                if pause.is_set() or (stop is not None and stop.is_set()):
                    return result
                if not self.store.publication_ready(candidate.job):
                    return result
                started = self.store.start_publication(
                    candidate.release.id,
                    candidate.release.sd_hash,
                )
                if started is None:
                    continue
                result += PublicationCycleResult(attempted=1)
                try:
                    submission = prepare_submission(self.settings, candidate)
                    response = self.client.publish(submission)
                except PublicationPaused as error:
                    self._retry(candidate, error.code, error.message)
                    pause.set()
                    self.logger.error("Index publication paused: %s", error.message)
                    self.record_event(
                        f"Index publication paused ({error.code}): {error.message}"
                    )
                    return result + PublicationCycleResult(retrying=1, paused=True)
                except RetryablePublicationError as error:
                    self._retry(
                        candidate,
                        error.code,
                        error.message,
                        retry_after=error.retry_after,
                    )
                    self.logger.warning(
                        "Index publication will retry %s: %s",
                        candidate.release.name,
                        error.message,
                    )
                    self.record_event(
                        f"Publication retry scheduled for {candidate.release.name}: "
                        f"{error.message}"
                    )
                    return result + PublicationCycleResult(retrying=1)
                except PublicationPreparationError as error:
                    self._retry(candidate, "local_artifact_error", str(error))
                    self.logger.error(
                        "Cannot prepare %s for Index publication: %s",
                        candidate.release.name,
                        error,
                    )
                    self.record_event(
                        f"Local publication artifact needs repair for "
                        f"{candidate.release.name}: {error}"
                    )
                    return result + PublicationCycleResult(retrying=1)
                except Exception as error:  # pragma: no cover - defensive boundary
                    self._retry(
                        candidate,
                        "publisher_error",
                        f"{type(error).__name__}: {error}",
                    )
                    self.logger.exception(
                        "Unexpected Index publisher failure for %s",
                        candidate.release.name,
                    )
                    return result + PublicationCycleResult(retrying=1)

                if response.state is PublicationState.REJECTED:
                    self._finish((candidate,), response)
                    result += PublicationCycleResult(rejected=1)
                    self.logger.error(
                        "Index rejected %s publication (%s): %s",
                        candidate.release.name,
                        response.error_code,
                        response.error_message,
                    )
                    self.record_event(
                        f"Index rejected {candidate.release.name} "
                        f"({response.error_code}): {response.error_message}"
                    )
                    continue

                completed_aliases = aliases[alias_index:]
                self._finish(completed_aliases, response)
                descriptor_finished = True
                if response.state is PublicationState.CONFLICT:
                    result += PublicationCycleResult(conflicts=len(completed_aliases))
                    self.logger.error(
                        "Index evidence conflict for %s (%s): %s",
                        candidate.release.name,
                        response.error_code,
                        response.error_message,
                    )
                    self.record_event(
                        f"INDEX EVIDENCE CONFLICT for {candidate.release.name} "
                        f"({response.error_code}): {response.error_message}"
                    )
                elif response.state is PublicationState.DUPLICATE:
                    result += PublicationCycleResult(duplicates=len(completed_aliases))
                    self.record_event(
                        f"Index linked {candidate.release.name} to the existing "
                        f"canonical torrent {response.artifact.btih}"
                    )
                else:
                    result += PublicationCycleResult(published=len(completed_aliases))
                    self.record_event(
                        f"Published {candidate.release.name} to the Index "
                        f"({response.outcome}; {response.artifact.btih})"
                    )
                break
            if not descriptor_finished:
                continue
        return result

    def _retry(
        self,
        candidate: PublicationCandidate,
        code: str,
        message: str,
        *,
        retry_after: float | None = None,
    ) -> None:
        self.store.retry_publication(
            candidate.release.id,
            candidate.release.sd_hash,
            code=code,
            error=message,
            retry_backoff=self.settings.retry_backoff,
            retry_after=retry_after,
        )

    def _finish(
        self,
        candidates: Sequence[PublicationCandidate],
        result: PublicationResult,
    ) -> None:
        artifact = result.artifact
        self.store.finish_publication(
            (
                (candidate.release.id, candidate.release.sd_hash)
                for candidate in candidates
            ),
            state=result.state,
            outcome=result.outcome,
            canonical=result.canonical,
            canonical_sha384=artifact.sha384 if artifact is not None else None,
            canonical_btih=artifact.btih if artifact is not None else None,
            canonical_torrent_url=(
                artifact.torrent_url if artifact is not None else None
            ),
            canonical_magnet_uri=(
                artifact.magnet_uri if artifact is not None else None
            ),
            winning_release_id=(
                artifact.winning_release_id if artifact is not None else None
            ),
            error_code=result.error_code,
            error=result.error_message,
        )


def prepare_submission(
    settings: Settings,
    candidate: PublicationCandidate,
) -> PublicationSubmission:
    release = candidate.release
    job = candidate.job
    if job.file_path is None or job.torrent_path is None:
        raise PublicationPreparationError("ledger has no payload or torrent path")
    if (
        job.sha384 is None
        or not SHA384_RE.fullmatch(job.sha384)
        or job.sha256 is None
        or not SHA256_RE.fullmatch(job.sha256)
        or job.info_hash is None
        or not BTIH_RE.fullmatch(job.info_hash)
    ):
        raise PublicationPreparationError("ledger digests are incomplete or invalid")
    if release.sha384 is not None and release.sha384 != job.sha384:
        raise PublicationPreparationError(
            "computed SHA-384 contradicts the Index claim"
        )

    try:
        payload_path = ensure_within(settings.data_dir, job.file_path)
        torrent_path = ensure_within(settings.outbox_dir, job.torrent_path)
    except ValueError as error:
        raise PublicationPreparationError(str(error)) from error
    if not payload_path.is_file() or payload_path.stat().st_size <= 0:
        raise PublicationPreparationError("verified payload is missing or empty")
    if not torrent_path.is_file():
        raise PublicationPreparationError("torrent metainfo is missing")
    if torrent_path.stat().st_size > MAX_TORRENT_BYTES:
        raise PublicationPreparationError("torrent exceeds the Index size limit")
    try:
        staged_torrent = torrent_path.read_bytes()
        staged = parse_torrent(staged_torrent)
        raw_torrent = strip_torrent_trackers(staged_torrent)
        parsed = parse_torrent(raw_torrent)
    except (OSError, TorrentError) as error:
        raise PublicationPreparationError(
            f"torrent cannot be parsed: {error}"
        ) from error
    if (
        staged.file_name != payload_path.name
        or staged.file_length != payload_path.stat().st_size
        or staged.info_hash != job.info_hash
        or staged.magnet_uri != job.magnet_uri
        or parsed.info_hash != staged.info_hash
        or parsed.trackers
    ):
        raise PublicationPreparationError("torrent contradicts the durable ledger")

    acquisition = _acquisition_transport(settings, candidate)
    manifest = {
        "schema": REQUEST_SCHEMA,
        "release": {"id": release.id},
        "lbry": {
            "sd_hash": release.sd_hash,
            "claimed_sha384": release.sha384,
        },
        "acquisition": {"transport": acquisition},
        "artifact": {
            "file_name": parsed.file_name,
            "size": parsed.file_length,
            "sha384": job.sha384,
            "sha256": job.sha256,
        },
        "torrent": {
            "file_name": parsed.file_name,
            "piece_length": parsed.piece_length,
            "piece_count": parsed.piece_count,
            "btih": parsed.info_hash,
            "sha256": parsed.torrent_sha256,
            "magnet_uri": parsed.magnet_uri,
            "trackers": list(parsed.trackers),
        },
    }
    try:
        encoded = encode_manifest(manifest)
    except ValueError as error:  # pragma: no cover - bounded fixed schema
        raise PublicationPreparationError(str(error)) from error
    return PublicationSubmission(
        release_id=release.id,
        sd_hash=release.sd_hash,
        sha384=job.sha384,
        btih=parsed.info_hash,
        payload_name=parsed.file_name,
        manifest=encoded,
        torrent=raw_torrent,
    )


def _acquisition_transport(
    settings: Settings,
    candidate: PublicationCandidate,
) -> str:
    manifest_path = (
        settings.outbox_dir
        / candidate.release.id
        / candidate.release.sd_hash
        / "manifest.json"
    )
    try:
        manifest_path = ensure_within(settings.outbox_dir, manifest_path)
        if (
            not manifest_path.is_file()
            or manifest_path.stat().st_size > MAX_MANIFEST_BYTES
        ):
            raise PublicationPreparationError("outbox manifest is missing or oversized")
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        if isinstance(error, PublicationPreparationError):
            raise
        raise PublicationPreparationError(
            f"outbox manifest cannot be read: {error}"
        ) from error
    if not isinstance(document, Mapping) or document.get("schema") != REQUEST_SCHEMA:
        raise PublicationPreparationError("outbox manifest schema is invalid")
    _expect_manifest_identity(document, candidate)
    acquisition = document.get("acquisition")
    if acquisition is None:
        return AcquisitionTransport.LBRY
    if not isinstance(acquisition, Mapping):
        raise PublicationPreparationError("outbox acquisition evidence is invalid")
    transport = acquisition.get("transport")
    if transport not in {AcquisitionTransport.LBRY, AcquisitionTransport.ODYSEE_CDN}:
        raise PublicationPreparationError("outbox acquisition transport is invalid")
    return str(transport)


def _expect_manifest_identity(
    document: Mapping[str, Any],
    candidate: PublicationCandidate,
) -> None:
    release = document.get("release")
    lbry = document.get("lbry")
    artifact = document.get("artifact")
    torrent = document.get("torrent")
    job = candidate.job
    if not all(
        isinstance(value, Mapping) for value in (release, lbry, artifact, torrent)
    ):
        raise PublicationPreparationError("outbox manifest sections are invalid")
    if (
        release.get("id") != candidate.release.id
        or lbry.get("sd_hash") != candidate.release.sd_hash
        or artifact.get("sha384") != job.sha384
        or artifact.get("sha256") != job.sha256
        or torrent.get("btih") != job.info_hash
    ):
        raise PublicationPreparationError("outbox manifest contradicts the ledger")


def _candidate_order(candidate: PublicationCandidate) -> tuple[float, str, str]:
    return (
        -candidate.release.popularity,
        candidate.release.id,
        candidate.release.sd_hash,
    )
