from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import Mock

from guncadmirror.models import JobState, PublicationBundle
from guncadmirror.paths import release_directory
from guncadmirror.pipeline import CycleResult, MirrorPipeline
from guncadmirror.publisher import OutboxPublisher
from guncadmirror.settings import Settings
from guncadmirror.state import JobStore

from .helpers import make_release


class StaticIndex:
    def __init__(self, releases: list[object]):
        self._releases = releases

    def releases(self) -> object:
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
    ) -> MirrorPipeline:
        return MirrorPipeline(
            self.settings,
            StaticIndex(releases),
            acquirer,
            self.store,
            publisher or self.publisher,
            disk_free=lambda _: disk_free,
        )

    def test_happy_path_is_verified_durable_and_idempotent(self) -> None:
        content = b"payload"
        release = make_release(content)
        payload = self.root / "lbry-download.zip"
        payload.write_bytes(content)
        acquirer = Mock()
        acquirer.acquire.return_value = payload
        pipeline = self._pipeline([release], acquirer)

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

        def restore_payload(*_args: object) -> Path:
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
        pipeline = self._pipeline([release], acquirer)

        with self.assertLogs("guncad-mirror.pipeline", level="ERROR"):
            self.assertEqual(pipeline.process(release), "failed")
        failed = self.store.get(release.id, release.sd_hash)
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertIn("LBRY broke", failed.last_error)
        self.assertEqual(failed.next_attempt_at, 110)

        self.assertEqual(pipeline.process(release), "skipped")
        self.assertEqual(acquirer.acquire.call_count, 1)
        self.now = 110
        self.assertEqual(pipeline.process(release), "ready")
        self.assertEqual(acquirer.acquire.call_count, 2)

    def test_policy_guards_skip_without_creating_jobs(self) -> None:
        release = make_release(channel="@blocked:b")
        acquirer = Mock()
        pipeline = self._pipeline([release], acquirer)
        pipeline.settings = Settings(
            endpoint=self.settings.endpoint,
            data_dir=self.root,
            min_free_space=0,
            blacklisted_handles=("blocked#",),
        )
        self.assertEqual(pipeline.process(release), "skipped")

        too_large = make_release(content=b"12345678", release_id="c" * 40)
        pipeline.settings = Settings(
            endpoint=self.settings.endpoint,
            data_dir=self.root,
            min_free_space=0,
            max_release_size=7,
        )
        self.assertEqual(pipeline.process(too_large), "skipped")

        pipeline.settings = self.settings
        pipeline.disk_free = lambda _: 13
        self.assertEqual(pipeline.process(release), "skipped")
        self.assertEqual(self.store.counts(), {})
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
        self.assertEqual(self.store.counts(), {"failed": 1})

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

    def test_cycle_stops_between_releases_after_current_work_is_durable(self) -> None:
        content = b"payload"
        first = make_release(content)
        second = make_release(content, release_id="c" * 40, sd_hash="d" * 96)
        payload = self.root / "payload.zip"
        payload.write_bytes(content)
        stop = Event()
        acquirer = Mock()

        def acquire(*_args: object) -> Path:
            stop.set()
            return payload

        acquirer.acquire.side_effect = acquire
        pipeline = self._pipeline([first, second], acquirer)

        with self.assertLogs("guncad-mirror.pipeline", level="INFO"):
            result = pipeline.run_cycle(stop)

        self.assertEqual(result, CycleResult(discovered=1, ready=1))
        self.assertEqual(acquirer.acquire.call_count, 1)
        with self.assertRaises(KeyError):
            self.store.get(second.id, second.sd_hash)

    def test_publisher_must_return_durable_files(self) -> None:
        content = b"payload"
        release = make_release(content)
        payload = self.root / "payload.zip"
        payload.write_bytes(content)
        acquirer = Mock()
        acquirer.acquire.return_value = payload

        publisher = Mock()

        def nondurable(release: object, hashes: object, torrent: object) -> object:
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
