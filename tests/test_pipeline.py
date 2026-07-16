from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock
from time import monotonic, sleep
from unittest.mock import Mock, patch

from guncadmirror.cancellation import AcquisitionCancelled
from guncadmirror.lbry import LbryProtocolError, LbryStreamUnavailable
from guncadmirror.models import JobState, PublicationBundle
from guncadmirror.odysee import OdyseeAcquisition
from guncadmirror.paths import release_directory
from guncadmirror.pipeline import CycleResult, MirrorPipeline
from guncadmirror.publisher import OutboxPublisher
from guncadmirror.settings import Settings
from guncadmirror.state import JobStore

from .helpers import make_release


class StaticIndex:
    def __init__(self, releases: list[object]):
        self._releases = releases

    def releases(self, **_kwargs: object) -> object:
        return iter(self._releases)


class MirrorPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = 100.0
        self.settings = Settings(
            endpoint="https://index.example/api/v2/releases/",
            data_dir=self.root,
            min_free_space=0,
            retry_backoff=10,
            torrent_piece_length=16 * 1024,
            torrent_trackers=("udp://tracker.example:80",),
        )
        self.store = JobStore(self.settings.state_path, clock=lambda: self.now)
        self.publisher = OutboxPublisher(self.settings.outbox_dir)

    def _pipeline(
        self,
        releases: list[object],
        acquirer: object,
        *,
        disk_free: int = 10**12,
        publisher: object | None = None,
        fallback: object | None = None,
        progress: object | None = None,
        record_event: Callable[[str], None] | None = None,
    ) -> MirrorPipeline:
        return MirrorPipeline(
            self.settings,
            StaticIndex(releases),
            acquirer,
            self.store,
            publisher or self.publisher,
            fallback_acquirer=fallback,
            disk_free=lambda _: disk_free,
            progress=progress,
            record_event=record_event,
        )

    def test_happy_path_is_verified_durable_and_idempotent(self) -> None:
        content = b"payload"
        release = make_release(content)
        payload = self.root / "lbry-download.zip"
        payload.write_bytes(content)
        acquirer = Mock()
        acquirer.acquire.return_value = payload
        progress = Mock()
        pipeline = self._pipeline([release], acquirer, progress=progress)

        self.assertEqual(pipeline.process(release), "ready")
        job = self.store.get(release.id, release.sd_hash)
        self.assertEqual(job.state, JobState.AWAITING_INDEX)
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.file_path, payload)
        self.assertTrue(job.torrent_path.is_file())
        manifest_path = (
            self.settings.outbox_dir / release.id / release.sd_hash / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["torrent"]["btih"], job.info_hash)

        metadata_path = (
            release_directory(
                self.settings.releases_dir,
                release.channel_handle,
                release.name,
                release.sd_hash,
            )
            / "release.json"
        )
        self.assertEqual(json.loads(metadata_path.read_text()), release.raw)

        self.assertEqual(pipeline.process(release), "skipped")
        self.assertEqual(acquirer.acquire.call_count, 1)
        self.assertEqual(
            self.store.get(release.id, release.sd_hash).attempts,
            1,
        )
        phases = [
            call.args[0].phase.value for call in progress.update_activity.call_args_list
        ]
        self.assertEqual(
            phases,
            [
                "Verifying plaintext",
                "Verifying plaintext",
                "Hashing BitTorrent pieces",
                "Hashing BitTorrent pieces",
                "Writing local outbox",
            ],
        )
        progress.clear_activity.assert_called_once_with(release)

    def test_missing_or_structurally_invalid_artifact_is_rebuilt(self) -> None:
        content = b"payload"
        release = make_release(content)
        payload = self.root / "payload.zip"
        payload.write_bytes(content)
        acquirer = Mock()
        acquirer.acquire.return_value = payload
        pipeline = self._pipeline([release], acquirer)
        self.assertEqual(pipeline.process(release), "ready")

        manifest_path = (
            self.settings.outbox_dir / release.id / release.sd_hash / "manifest.json"
        )
        manifest_path.unlink()
        self.assertEqual(pipeline.process(release), "ready")
        self.assertEqual(acquirer.acquire.call_count, 2)

        manifest_path.write_text("{}")
        self.assertEqual(pipeline.process(release), "ready")
        self.assertEqual(acquirer.acquire.call_count, 3)

        torrent_path = self.store.get(release.id, release.sd_hash).torrent_path
        self.assertIsNotNone(torrent_path)
        torrent_path.write_bytes(b"")
        self.assertEqual(pipeline.process(release), "ready")
        self.assertEqual(acquirer.acquire.call_count, 4)

        torrent_path.write_bytes(b"nonempty corruption")
        self.assertEqual(pipeline.process(release), "ready")
        self.assertEqual(acquirer.acquire.call_count, 5)

        payload.write_bytes(b"x")

        def restore_payload(*_args: object, **_kwargs: object) -> Path:
            payload.write_bytes(content)
            return payload

        acquirer.acquire.side_effect = restore_payload
        self.assertEqual(pipeline.process(release), "ready")
        self.assertEqual(acquirer.acquire.call_count, 6)

        renamed_release = make_release(content, name="Renamed Release")
        self.assertEqual(pipeline.process(renamed_release), "ready")
        self.assertEqual(acquirer.acquire.call_count, 7)
        self.assertEqual(
            json.loads(manifest_path.read_text())["release"]["name"],
            "Renamed Release",
        )
        self.assertEqual(
            self.store.get(release.id, release.sd_hash).attempts,
            7,
        )

    def test_identical_payloads_keep_distinct_publication_jobs(self) -> None:
        content = b"payload"
        first = make_release(content)
        second = make_release(content, release_id="c" * 40, sd_hash="d" * 96)
        payload = self.root / "payload.zip"
        payload.write_bytes(content)
        acquirer = Mock()
        acquirer.acquire.return_value = payload
        pipeline = self._pipeline([first, second], acquirer)

        self.assertEqual(pipeline.run_cycle().ready, 2)
        manifests = [
            self.settings.outbox_dir / release.id / release.sd_hash / "manifest.json"
            for release in (first, second)
        ]
        self.assertTrue(all(path.is_file() for path in manifests))
        self.assertEqual(
            [json.loads(path.read_text())["release"]["id"] for path in manifests],
            [first.id, second.id],
        )

    def test_legacy_payload_records_computed_evidence_and_is_idempotent(self) -> None:
        content = b"legacy payload"
        release = replace(make_release(content), size=None, sha384=None)
        payload = self.root / "legacy.zip"
        payload.write_bytes(content)
        acquirer = Mock()
        acquirer.acquire.return_value = payload
        pipeline = self._pipeline([release], acquirer)

        self.assertEqual(pipeline.process(release), "ready")
        job = self.store.get(release.id, release.sd_hash)
        manifest = json.loads(
            (
                self.settings.outbox_dir
                / release.id
                / release.sd_hash
                / "manifest.json"
            ).read_text()
        )
        self.assertIsNone(manifest["lbry"]["claimed_sha384"])
        self.assertEqual(manifest["artifact"]["size"], len(content))
        self.assertEqual(manifest["artifact"]["sha384"], job.sha384)
        self.assertEqual(pipeline.process(release), "skipped")
        acquirer.acquire.assert_called_once()

    def test_release_failure_is_recorded_and_obeys_retry_backoff(self) -> None:
        content = b"payload"
        release = make_release(content)
        payload = self.root / "payload.zip"
        payload.write_bytes(content)
        acquirer = Mock()
        acquirer.acquire.side_effect = [RuntimeError("LBRY broke"), payload]
        events: list[str] = []
        pipeline = self._pipeline([release], acquirer, record_event=events.append)

        with self.assertLogs("guncad-mirror.pipeline", level="ERROR"):
            self.assertEqual(pipeline.process(release), "failed")
        failed = self.store.get(release.id, release.sd_hash)
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertIn("LBRY broke", failed.last_error)
        self.assertEqual(failed.next_attempt_at, 110)
        self.assertIn("FAILED @channel:c/Release Name", events[0])
        self.assertIn("RuntimeError: LBRY broke", events[0])

        self.assertEqual(pipeline.process(release), "skipped")
        self.assertEqual(acquirer.acquire.call_count, 1)
        self.now = 110
        self.assertEqual(pipeline.process(release), "ready")
        self.assertEqual(acquirer.acquire.call_count, 2)

    def test_limited_odysee_fallback_records_transport_and_lbry_failure(self) -> None:
        content = b"payload"
        release = make_release(content)
        payload = self.root / "odysee-payload.zip"
        payload.write_bytes(content)
        acquirer = Mock()
        acquirer.acquire.side_effect = LbryStreamUnavailable("no blob peers")
        fallback = Mock()
        fallback.acquire.return_value = OdyseeAcquisition(
            payload,
            (
                "https://player.odycdn.com/v6/streams/"
                f"{release.id}/{release.sd_hash[:6]}.zip"
            ),
        )
        pipeline = self._pipeline([release], acquirer, fallback=fallback)

        with self.assertLogs("guncad-mirror.pipeline", level="WARNING"):
            self.assertEqual(pipeline.process(release), "ready")

        manifest = json.loads(
            (
                self.settings.outbox_dir
                / release.id
                / release.sd_hash
                / "manifest.json"
            ).read_text()
        )
        self.assertEqual(manifest["acquisition"]["transport"], "odysee-cdn")
        self.assertIn("no blob peers", manifest["acquisition"]["lbry_failure"])
        self.assertEqual(pipeline.process(release), "skipped")
        fallback.acquire.assert_called_once_with(
            release,
            release_directory(
                self.settings.releases_dir,
                release.channel_handle,
                release.name,
                release.sd_hash,
            ),
            stop=None,
        )

        manifest["acquisition"]["transport"] = "mystery"
        manifest_path = (
            self.settings.outbox_dir / release.id / release.sd_hash / "manifest.json"
        )
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual(pipeline.process(release), "ready")

    def test_odysee_fallback_fails_closed_without_claims_or_on_second_failure(
        self,
    ) -> None:
        content = b"payload"
        release = make_release(content)
        legacy = replace(release, size=None, sha384=None)
        acquirer = Mock()
        acquirer.acquire.side_effect = LbryStreamUnavailable("no peers")
        fallback = Mock()
        fallback.acquire.side_effect = RuntimeError("CDN down")
        pipeline = self._pipeline([legacy], acquirer, fallback=fallback)

        with self.assertLogs("guncad-mirror.pipeline", level="ERROR"):
            self.assertEqual(pipeline.process(legacy), "failed")
        fallback.acquire.assert_not_called()

        second = make_release(content, release_id="c" * 40, sd_hash="d" * 96)
        with self.assertLogs("guncad-mirror.pipeline", level="WARNING"):
            self.assertEqual(pipeline.process(second), "failed")
        fallback.acquire.assert_called_once()
        self.assertIn(
            "Odysee fallback also failed",
            self.store.get(second.id, second.sd_hash).last_error,
        )

        contradictory = make_release(content, release_id="e" * 40, sd_hash="f" * 96)
        acquirer.acquire.side_effect = LbryProtocolError("claim drift")
        fallback.reset_mock()
        with self.assertLogs("guncad-mirror.pipeline", level="ERROR"):
            self.assertEqual(pipeline.process(contradictory), "failed")
        fallback.acquire.assert_not_called()

    def test_bad_odysee_plaintext_is_deleted_before_retry(self) -> None:
        release = make_release(b"correct")
        payload = self.root / "bad-cdn.zip"
        payload.write_bytes(b"corrupt")
        acquirer = Mock()
        acquirer.acquire.side_effect = LbryStreamUnavailable("no peers")
        fallback = Mock()
        fallback.acquire.return_value = OdyseeAcquisition(
            payload,
            (
                "https://player.odycdn.com/v6/streams/"
                f"{release.id}/{release.sd_hash[:6]}.zip"
            ),
        )
        pipeline = self._pipeline([release], acquirer, fallback=fallback)

        with self.assertLogs("guncad-mirror.pipeline", level="WARNING"):
            self.assertEqual(pipeline.process(release), "failed")

        self.assertFalse(payload.exists())

    def test_policy_guards_record_durable_exclusions_without_acquiring(self) -> None:
        release = make_release(channel="@blocked:b")
        acquirer = Mock()
        events: list[str] = []
        pipeline = self._pipeline([release], acquirer, record_event=events.append)
        pipeline.settings = Settings(
            endpoint=self.settings.endpoint,
            data_dir=self.root,
            min_free_space=0,
            blacklisted_handles=("blocked#",),
        )
        self.assertEqual(pipeline.process(release), "skipped")
        blocked = self.store.get(release.id, release.sd_hash)
        self.assertEqual(blocked.state, JobState.EXCLUDED)
        self.assertIn("configured blacklist", blocked.exclusion_reason)

        too_large = make_release(content=b"12345678", release_id="c" * 40)
        pipeline.settings = Settings(
            endpoint=self.settings.endpoint,
            data_dir=self.root,
            min_free_space=0,
            max_release_size=7,
        )
        self.assertEqual(pipeline.process(too_large), "skipped")
        oversized = self.store.get(too_large.id, too_large.sd_hash)
        self.assertEqual(oversized.state, JobState.EXCLUDED)
        self.assertIn("exceeds configured maximum", oversized.exclusion_reason)

        # Repeated scans retain the exclusion without duplicating its event.
        self.assertEqual(pipeline.process(too_large), "skipped")
        self.assertEqual(len(events), 2)

        pipeline.settings = self.settings
        pipeline.disk_free = lambda _: 13
        self.assertEqual(pipeline.process(release), "skipped")
        self.assertEqual(self.store.counts(), {"excluded": 2})
        acquirer.acquire.assert_not_called()

        unknown_size = replace(too_large, size=None, sha384=None)
        pipeline.settings = Settings(
            endpoint=self.settings.endpoint,
            data_dir=self.root,
            min_free_space=0,
            max_release_size=7,
        )
        acquirer.acquire.return_value = self.root / "unknown.bin"
        acquirer.acquire.return_value.write_bytes(b"12345678")
        with self.assertLogs("guncad-mirror.pipeline", level="ERROR"):
            self.assertEqual(pipeline.process(unknown_size), "failed")
        self.assertIn(
            "exceeding configured maximum",
            self.store.get(unknown_size.id, unknown_size.sd_hash).last_error,
        )
        self.assertEqual(self.store.counts(), {"excluded": 1, "failed": 1})

    def test_new_size_policy_does_not_demote_a_completed_job(self) -> None:
        release = make_release(content=b"12345678")
        payload = self.root / "completed.zip"
        payload.write_bytes(b"12345678")
        acquirer = Mock()
        acquirer.acquire.return_value = payload
        pipeline = self._pipeline([release], acquirer)

        self.assertEqual(pipeline.process(release), "ready")
        pipeline.settings = Settings(
            endpoint=self.settings.endpoint,
            data_dir=self.root,
            min_free_space=0,
            max_release_size=7,
        )

        self.assertEqual(pipeline.process(release), "skipped")
        self.assertEqual(
            self.store.get(release.id, release.sd_hash).state,
            JobState.AWAITING_INDEX,
        )
        acquirer.acquire.assert_called_once()

    def test_cycle_isolates_release_failures_and_counts_outcomes(self) -> None:
        content = b"payload"
        good = make_release(content)
        broken = make_release(content, release_id="c" * 40, sd_hash="d" * 96)
        skipped = make_release(
            content, release_id="e" * 40, sd_hash="f" * 96, channel="@blocked:b"
        )
        payload = self.root / "payload.zip"
        payload.write_bytes(content)
        acquirer = Mock()
        acquirer.acquire.side_effect = [payload, RuntimeError("no peers")]
        pipeline = self._pipeline([good, broken, skipped], acquirer)
        pipeline.settings = Settings(
            endpoint=self.settings.endpoint,
            data_dir=self.root,
            min_free_space=0,
            retry_backoff=10,
            blacklisted_handles=("blocked#",),
            torrent_piece_length=16 * 1024,
        )

        with self.assertLogs("guncad-mirror.pipeline", level="ERROR"):
            result = pipeline.run_cycle()
        self.assertEqual(
            result,
            CycleResult(discovered=3, ready=1, skipped=1, failed=1),
        )
        self.assertEqual(CycleResult().add("unknown").discovered, 1)

    def test_cycle_runs_four_lbry_acquisitions_concurrently(self) -> None:
        content = b"payload"
        releases = [
            make_release(
                content,
                release_id=f"{index + 1:040x}",
                sd_hash=f"{index + 100:096x}",
                name=f"Release {index}",
            )
            for index in range(8)
        ]
        payload = self.root / "payload.zip"
        payload.write_bytes(content)
        gate = Event()
        saturated = Event()
        lock = Lock()
        active = 0
        peak = 0

        class BlockingAcquirer:
            def acquire(self, *_args: object, **_kwargs: object) -> Path:
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                    if active == 4:
                        saturated.set()
                try:
                    if not gate.wait(5):
                        raise TimeoutError("test did not release LBRY workers")
                    return payload
                finally:
                    with lock:
                        active -= 1

        pipeline = self._pipeline(releases, BlockingAcquirer())
        with ThreadPoolExecutor(max_workers=1) as executor:
            cycle = executor.submit(pipeline.run_cycle)
            try:
                self.assertTrue(saturated.wait(2))
                self.assertEqual(peak, 4)
            finally:
                gate.set()
            result = cycle.result(timeout=10)

        self.assertEqual(result, CycleResult(discovered=8, ready=8))
        self.assertEqual(peak, 4)

    def test_odysee_fallback_has_an_independent_two_worker_stage(self) -> None:
        content = b"payload"
        releases = [
            make_release(
                content,
                release_id=f"{index + 1:040x}",
                sd_hash=f"{index + 100:096x}",
                name=f"Release {index}",
            )
            for index in range(8)
        ]
        payload = self.root / "odysee-payload.zip"
        payload.write_bytes(content)
        gate = Event()
        saturated = Event()
        all_lbry_attempted = Event()
        lock = Lock()
        lbry_calls = 0
        odysee_active = 0
        odysee_peak = 0

        class UnavailableLbry:
            def acquire(self, *_args: object, **_kwargs: object) -> Path:
                nonlocal lbry_calls
                with lock:
                    lbry_calls += 1
                    if lbry_calls == len(releases):
                        all_lbry_attempted.set()
                raise LbryStreamUnavailable("no peers")

        class BlockingOdysee:
            def acquire(
                self,
                release: object,
                *_args: object,
                **_kwargs: object,
            ) -> OdyseeAcquisition:
                nonlocal odysee_active, odysee_peak
                with lock:
                    odysee_active += 1
                    odysee_peak = max(odysee_peak, odysee_active)
                    if odysee_active == 2:
                        saturated.set()
                try:
                    if not gate.wait(5):
                        raise TimeoutError("test did not release Odysee workers")
                    return OdyseeAcquisition(
                        payload,
                        f"https://player.odycdn.com/{release.id}",
                    )
                finally:
                    with lock:
                        odysee_active -= 1

        pipeline = self._pipeline(
            releases,
            UnavailableLbry(),
            fallback=BlockingOdysee(),
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            cycle = executor.submit(pipeline.run_cycle)
            try:
                self.assertTrue(saturated.wait(2))
                self.assertTrue(all_lbry_attempted.wait(2))
                self.assertEqual(odysee_peak, 2)
            finally:
                gate.set()
            result = cycle.result(timeout=10)

        self.assertEqual(result, CycleResult(discovered=8, ready=8))
        self.assertEqual(lbry_calls, 8)
        self.assertEqual(odysee_peak, 2)

    def test_disk_reservation_contention_waits_instead_of_skipping(self) -> None:
        content = b"payload"
        releases = [
            make_release(
                content,
                release_id=f"{index + 1:040x}",
                sd_hash=f"{index + 100:096x}",
                name=f"Release {index}",
            )
            for index in range(2)
        ]
        payload = self.root / "payload.zip"
        payload.write_bytes(content)
        gate = Event()
        first_started = Event()
        second_started = Event()
        calls = 0

        class BlockingAcquirer:
            def acquire(self, *_args: object, **_kwargs: object) -> Path:
                nonlocal calls
                calls += 1
                if calls == 1:
                    first_started.set()
                    if not gate.wait(5):
                        raise TimeoutError("test did not release disk reservation")
                else:
                    second_started.set()
                return payload

        pipeline = self._pipeline(
            releases,
            BlockingAcquirer(),
            disk_free=20,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            cycle = executor.submit(pipeline.run_cycle)
            try:
                self.assertTrue(first_started.wait(2))
                self.assertFalse(second_started.wait(0.1))
            finally:
                gate.set()
            result = cycle.result(timeout=10)

        self.assertEqual(result, CycleResult(discovered=2, ready=2))
        self.assertTrue(second_started.is_set())

    def test_stop_cancels_queued_workers_without_recording_failures(self) -> None:
        content = b"payload"
        releases = [
            make_release(
                content,
                release_id=f"{index + 1:040x}",
                sd_hash=f"{index + 100:096x}",
                name=f"Release {index}",
            )
            for index in range(3)
        ]
        stop = Event()
        started = Event()

        class CancellableAcquirer:
            def acquire(
                self,
                *_args: object,
                stop: Event | None = None,
                **_kwargs: object,
            ) -> Path:
                started.set()
                if stop is None or not stop.wait(5):
                    raise TimeoutError("test did not request cancellation")
                raise AcquisitionCancelled("stop")

        pipeline = self._pipeline(releases, CancellableAcquirer())
        pipeline.settings = replace(
            self.settings,
            lbry_concurrency=1,
            odysee_concurrency=1,
            finalize_concurrency=1,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            cycle = executor.submit(pipeline.run_cycle, stop)
            self.assertTrue(started.wait(2))
            deadline = monotonic() + 2
            while self.store.counts().get("acquiring") != 3 and monotonic() < deadline:
                sleep(0.01)
            self.assertEqual(self.store.counts(), {"acquiring": 3})
            stop.set()
            result = cycle.result(timeout=10)

        self.assertEqual(result, CycleResult(discovered=3, stopped=3))
        self.assertEqual(self.store.counts(), {"acquiring": 3})

    def test_cycle_deduplicates_an_index_job_before_submitting_it(self) -> None:
        content = b"payload"
        release = make_release(content)
        payload = self.root / "payload.zip"
        payload.write_bytes(content)
        acquirer = Mock(return_value=payload)
        acquirer.acquire.return_value = payload
        pipeline = self._pipeline([release, release], acquirer)

        with self.assertLogs("guncad-mirror.pipeline", level="WARNING"):
            result = pipeline.run_cycle()

        self.assertEqual(result, CycleResult(discovered=2, ready=1, skipped=1))
        acquirer.acquire.assert_called_once()

    def test_operator_event_failure_cannot_break_release_handling(self) -> None:
        release = make_release()
        callback = Mock(side_effect=RuntimeError("event sink broke"))
        pipeline = self._pipeline(
            [release],
            Mock(),
            disk_free=13,
            record_event=callback,
        )

        with self.assertLogs("guncad-mirror.pipeline", level="ERROR"):
            self.assertEqual(pipeline.process(release), "skipped")

        callback.assert_called_once()
        self.assertEqual(self.store.counts(), {})

    def test_cycle_cancels_current_release_without_recording_a_failure(self) -> None:
        content = b"payload"
        first = make_release(content)
        second = make_release(content, release_id="c" * 40, sd_hash="d" * 96)
        payload = self.root / "payload.zip"
        payload.write_bytes(content)
        stop = Event()
        acquirer = Mock()

        def acquire(*_args: object, **_kwargs: object) -> Path:
            stop.set()
            return payload

        acquirer.acquire.side_effect = acquire
        pipeline = self._pipeline([first, second], acquirer)
        pipeline.settings = replace(
            self.settings,
            lbry_concurrency=1,
            odysee_concurrency=1,
            finalize_concurrency=1,
        )

        with self.assertLogs("guncad-mirror.pipeline", level="INFO"):
            result = pipeline.run_cycle(stop)

        self.assertEqual(result, CycleResult(discovered=1, stopped=1))
        self.assertEqual(acquirer.acquire.call_count, 1)
        first_job = self.store.get(first.id, first.sd_hash)
        self.assertEqual(first_job.state, JobState.ACQUIRING)
        self.assertIsNone(first_job.last_error)
        with self.assertRaises(KeyError):
            self.store.get(second.id, second.sd_hash)

    def test_cancellation_from_fallback_or_torrent_remains_retryable(self) -> None:
        content = b"payload"
        fallback_release = make_release(content)
        torrent_release = make_release(
            content,
            release_id="c" * 40,
            sd_hash="d" * 96,
        )
        payload = self.root / "payload.zip"
        payload.write_bytes(content)
        acquirer = Mock()
        acquirer.acquire.side_effect = [
            LbryStreamUnavailable("no peers"),
            payload,
        ]
        fallback = Mock()
        fallback.acquire.side_effect = AcquisitionCancelled("stop")
        pipeline = self._pipeline(
            [fallback_release, torrent_release],
            acquirer,
            fallback=fallback,
        )

        with self.assertLogs("guncad-mirror.pipeline", level="INFO"):
            self.assertEqual(pipeline.process(fallback_release), "stopped")
        first_job = self.store.get(fallback_release.id, fallback_release.sd_hash)
        self.assertEqual(first_job.state, JobState.ACQUIRING)
        self.assertIsNone(first_job.last_error)

        with (
            patch(
                "guncadmirror.pipeline.create_torrent",
                side_effect=AcquisitionCancelled("stop"),
            ),
            self.assertLogs("guncad-mirror.pipeline", level="INFO"),
        ):
            self.assertEqual(pipeline.process(torrent_release), "stopped")
        second_job = self.store.get(torrent_release.id, torrent_release.sd_hash)
        self.assertEqual(second_job.state, JobState.VERIFIED)
        self.assertIsNone(second_job.last_error)

    def test_cycle_reports_stop_during_index_page_acquisition(self) -> None:
        index = Mock()
        index.releases.side_effect = AcquisitionCancelled("stop")
        pipeline = MirrorPipeline(
            self.settings,
            index,
            Mock(),
            self.store,
            self.publisher,
            disk_free=lambda _: 10**12,
        )

        with self.assertLogs("guncad-mirror.pipeline", level="INFO"):
            result = pipeline.run_cycle(Event())

        self.assertEqual(result, CycleResult(stopped=1))

    def test_publisher_must_return_durable_files(self) -> None:
        content = b"payload"
        release = make_release(content)
        payload = self.root / "payload.zip"
        payload.write_bytes(content)
        acquirer = Mock()
        acquirer.acquire.return_value = payload

        publisher = Mock()

        def nondurable(
            release: object,
            hashes: object,
            torrent: object,
            acquisition: object,
        ) -> object:
            return PublicationBundle(
                release=release,
                hashes=hashes,
                torrent=torrent,
                manifest_path=self.root / "missing-manifest.json",
            )

        publisher.publish.side_effect = nondurable
        pipeline = self._pipeline([release], acquirer, publisher=publisher)
        with self.assertLogs("guncad-mirror.pipeline", level="ERROR"):
            self.assertEqual(pipeline.process(release), "failed")
        job = self.store.get(release.id, release.sd_hash)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertIn("durable outbox", job.last_error)


if __name__ == "__main__":
    unittest.main()
