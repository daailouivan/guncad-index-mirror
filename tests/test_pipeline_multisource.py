from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from guncadmirror.github import GitHubAcquirer, GitHubAcquisition
from guncadmirror.http_acquirer import HttpAcquirer, HttpAcquisition
from guncadmirror.models import AcquisitionTransport, JobState, Release
from guncadmirror.pipeline import MirrorPipeline
from guncadmirror.printables import PrintablesAcquirer, PrintablesAcquisition
from guncadmirror.publisher import OutboxPublisher
from guncadmirror.settings import Settings
from guncadmirror.state import JobStore
from guncadmirror.torrent_acquirer import TorrentAcquirer, TorrentAcquisition


class PipelineMultiSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.settings = Settings(
            endpoint="https://example.com/api/v2/releases/",
            data_dir=self.root,
            min_free_space=0,
            torrent_piece_length=16 * 1024,
        )
        self.store = JobStore(self.settings.state_path)
        self.publisher = OutboxPublisher(self.settings.outbox_dir)
        self.index_client = MagicMock()
        self.lbry_acquirer = MagicMock()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_pipeline_processes_printables_release_end_to_end(self) -> None:
        payload = {
            "id": "printables-1863745",
            "name": "AmmoBox 22LR",
            "channel": {"handle": "MarcinMJessa_5279187"},
            "origin": {
                "platform": "printables",
                "external_id": "1863745",
                "checksum": "7787145",
                "size": 50,
                "popularity": 1.25,
                "links": [
                    {
                        "name": "Printables",
                        "url": "https://printables.com/model/1863745-ammobox-22lr",
                    }
                ],
            },
        }
        release = Release.from_api(payload)

        # Mock PrintablesAcquirer
        mock_printables = MagicMock(spec=PrintablesAcquirer)

        def fake_acquire(
            rel: Release, output_dir: Path, **kwargs: object
        ) -> PrintablesAcquisition:
            dest = output_dir / "22LR_AmmoBox.3mf"
            dest.write_bytes(b"A" * 50)
            return PrintablesAcquisition(
                path=dest, source_url="https://files.printables.com/file.3mf"
            )

        mock_printables.acquire.side_effect = fake_acquire

        pipeline = MirrorPipeline(
            self.settings,
            self.index_client,
            self.lbry_acquirer,
            self.store,
            self.publisher,
            printables_acquirer=mock_printables,
        )

        outcome = pipeline.process(release)
        self.assertEqual(outcome, "ready")

        # Verify job is recorded in SQLite
        job = self.store.get(release.id, release.sd_hash)
        self.assertIsNotNone(job)
        self.assertEqual(job.state, JobState.AWAITING_INDEX)
        self.assertEqual(job.release_id, "printables-1863745")
        self.assertEqual(job.sd_hash, release.sd_hash)

        # Verify outbox torrent and manifest exist
        outbox_entry = self.settings.outbox_dir / release.id / release.sd_hash
        manifest_path = outbox_entry / "manifest.json"
        self.assertTrue(manifest_path.is_file())

        with manifest_path.open() as f:
            manifest = json.load(f)

        self.assertEqual(manifest["release"]["id"], "printables-1863745")
        self.assertEqual(manifest["release"]["platform"], "printables")
        self.assertEqual(
            manifest["acquisition"]["transport"], AcquisitionTransport.PRINTABLES
        )
        self.assertEqual(
            manifest["acquisition"]["source_url"],
            "https://files.printables.com/file.3mf",
        )
        self.assertEqual(manifest["artifact"]["size"], 50)

        torrent_files = list(outbox_entry.glob("*.torrent"))
        self.assertEqual(len(torrent_files), 1)

    def test_pipeline_processes_github_release_end_to_end(self) -> None:
        payload = {
            "id": "github-org-coolpart-v2",
            "name": "Cool Part V2",
            "channel": {"handle": "org"},
            "origin": {
                "platform": "github",
                "external_id": "org/coolpart",
                "size": 60,
                "popularity": 2.0,
                "links": [
                    {
                        "name": "GitHub",
                        "url": "https://github.com/org/coolpart/releases/tag/v2.0",
                    }
                ],
            },
        }
        release = Release.from_api(payload)

        # Mock GitHubAcquirer
        mock_github = MagicMock(spec=GitHubAcquirer)

        def fake_acquire(
            rel: Release, output_dir: Path, **kwargs: object
        ) -> GitHubAcquisition:
            dest = output_dir / "coolpart.zip"
            dest.write_bytes(b"B" * 60)
            return GitHubAcquisition(
                path=dest, source_url="https://github.com/org/coolpart/asset.zip"
            )

        mock_github.acquire.side_effect = fake_acquire

        pipeline = MirrorPipeline(
            self.settings,
            self.index_client,
            self.lbry_acquirer,
            self.store,
            self.publisher,
            github_acquirer=mock_github,
        )

        outcome = pipeline.process(release)
        self.assertEqual(outcome, "ready")

        # Verify job is recorded in SQLite
        job = self.store.get(release.id, release.sd_hash)
        self.assertIsNotNone(job)
        self.assertEqual(job.state, JobState.AWAITING_INDEX)

        # Verify outbox manifest
        outbox_entry = self.settings.outbox_dir / release.id / release.sd_hash
        manifest_path = outbox_entry / "manifest.json"
        self.assertTrue(manifest_path.is_file())

        with manifest_path.open() as f:
            manifest = json.load(f)

        self.assertEqual(manifest["release"]["platform"], "github")
        self.assertEqual(
            manifest["acquisition"]["transport"], AcquisitionTransport.GITHUB
        )

    def test_pipeline_processes_http_release_end_to_end(self) -> None:
        payload = {
            "id": "http-direct-frame-v1",
            "name": "Direct Frame Model",
            "channel": {"handle": "direct-cad"},
            "origin": {
                "platform": "http",
                "external_id": "direct-frame-v1",
                "size": 80,
                "popularity": 1.5,
                "links": [
                    {
                        "name": "Download",
                        "url": "https://example.com/files/frame.stl",
                        "download": True,
                    }
                ],
            },
        }
        release = Release.from_api(payload)

        # Mock HttpAcquirer
        mock_http = MagicMock(spec=HttpAcquirer)

        def fake_acquire(
            rel: Release, output_dir: Path, **kwargs: object
        ) -> HttpAcquisition:
            dest = output_dir / "frame.stl"
            dest.write_bytes(b"H" * 80)
            return HttpAcquisition(
                path=dest, source_url="https://example.com/files/frame.stl"
            )

        mock_http.acquire.side_effect = fake_acquire

        pipeline = MirrorPipeline(
            self.settings,
            self.index_client,
            self.lbry_acquirer,
            self.store,
            self.publisher,
            http_acquirer=mock_http,
        )

        outcome = pipeline.process(release)
        self.assertEqual(outcome, "ready")

        # Verify job is recorded in SQLite
        job = self.store.get(release.id, release.sd_hash)
        self.assertIsNotNone(job)
        self.assertEqual(job.state, JobState.AWAITING_INDEX)

        # Verify outbox manifest
        outbox_entry = self.settings.outbox_dir / release.id / release.sd_hash
        manifest_path = outbox_entry / "manifest.json"
        self.assertTrue(manifest_path.is_file())

        with manifest_path.open() as f:
            manifest = json.load(f)

        self.assertEqual(manifest["release"]["platform"], "http")
        self.assertEqual(
            manifest["acquisition"]["transport"], AcquisitionTransport.HTTP
        )
        self.assertEqual(
            manifest["acquisition"]["source_url"], "https://example.com/files/frame.stl"
        )
        self.assertEqual(manifest["artifact"]["size"], 80)

    def test_pipeline_processes_torrent_release_end_to_end(self) -> None:
        payload = {
            "id": "torrent-external-receiver",
            "name": "Receiver Package",
            "channel": {"handle": "swarm-channel"},
            "origin": {
                "platform": "torrent",
                "external_id": "external-receiver",
                "size": 120,
                "popularity": 1.8,
                "links": [
                    {
                        "name": "Magnet",
                        "url": "magnet:?xt=urn:btih:ff54e65a94386a8375b836a294dc9333e0c393cb&dn=receiver.zip",
                    }
                ],
            },
        }
        release = Release.from_api(payload)

        # Mock TorrentAcquirer
        mock_torrent = MagicMock(spec=TorrentAcquirer)

        def fake_acquire(
            rel: Release, output_dir: Path, **kwargs: object
        ) -> TorrentAcquisition:
            dest = output_dir / "receiver.zip"
            dest.write_bytes(b"T" * 120)
            return TorrentAcquisition(
                path=dest,
                source_url="magnet:?xt=urn:btih:ff54e65a94386a8375b836a294dc9333e0c393cb&dn=receiver.zip",
            )

        mock_torrent.acquire.side_effect = fake_acquire

        pipeline = MirrorPipeline(
            self.settings,
            self.index_client,
            self.lbry_acquirer,
            self.store,
            self.publisher,
            torrent_acquirer=mock_torrent,
        )

        outcome = pipeline.process(release)
        self.assertEqual(outcome, "ready")

        # Verify job is recorded in SQLite
        job = self.store.get(release.id, release.sd_hash)
        self.assertIsNotNone(job)
        self.assertEqual(job.state, JobState.AWAITING_INDEX)

        # Verify outbox manifest
        outbox_entry = self.settings.outbox_dir / release.id / release.sd_hash
        manifest_path = outbox_entry / "manifest.json"
        self.assertTrue(manifest_path.is_file())

        with manifest_path.open() as f:
            manifest = json.load(f)

        self.assertEqual(manifest["release"]["platform"], "torrent")
        self.assertEqual(
            manifest["acquisition"]["transport"], AcquisitionTransport.TORRENT
        )
        self.assertIn("magnet:?xt=urn:btih:", manifest["acquisition"]["source_url"])
        self.assertEqual(manifest["artifact"]["size"], 120)

    def test_printables_size_mismatch_with_index_estimate_is_marked_already_prepared(self) -> None:
        payload = {
            "id": "printables-9999",
            "name": "Model Size Mismatch",
            "channel": {"handle": "designer_123"},
            "origin": {
                "platform": "printables",
                "external_id": "9999",
                "size": 5000,  # Uncompressed estimate from GunCAD Index
                "links": [{"name": "Printables", "url": "https://printables.com/model/9999"}],
            },
        }
        release = Release.from_api(payload)
        mock_printables = MagicMock(spec=PrintablesAcquirer)

        def fake_acquire(rel: Release, output_dir: Path, **kwargs: object) -> PrintablesAcquisition:
            dest = output_dir / "model.3mf"
            dest.write_bytes(b"Z" * 1500)  # Actual downloaded file size is 1500 != 5000
            return PrintablesAcquisition(
                path=dest, source_url="https://files.printables.com/model.3mf"
            )

        mock_printables.acquire.side_effect = fake_acquire

        pipeline = MirrorPipeline(
            self.settings,
            self.index_client,
            self.lbry_acquirer,
            self.store,
            self.publisher,
            printables_acquirer=mock_printables,
        )

        outcome = pipeline.process(release)
        self.assertEqual(outcome, "ready")

        job = self.store.get(release.id, release.sd_hash)
        self.assertEqual(job.state, JobState.AWAITING_INDEX)
        self.assertEqual(job.payload_size, 1500)

        # Re-running process() on the same release must recognize it as already prepared
        outcome2 = pipeline.process(release)
        self.assertEqual(outcome2, "skipped")

        # Job must stay awaiting_index and not be demoted to acquiring!
        job_after = self.store.get(release.id, release.sd_hash)
        self.assertEqual(job_after.state, JobState.AWAITING_INDEX)

    def test_per_source_concurrency_allows_github_while_printables_is_saturated(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        p1 = Release.from_api({
            "id": "printables-block-1",
            "name": "Printables Block 1",
            "channel": {"handle": "maker1"},
            "origin": {"platform": "printables", "external_id": "p1", "links": []},
        })
        p2 = Release.from_api({
            "id": "printables-block-2",
            "name": "Printables Block 2",
            "channel": {"handle": "maker2"},
            "origin": {"platform": "printables", "external_id": "p2", "links": []},
        })
        gh = Release.from_api({
            "id": "github-repo-fast",
            "name": "Fast GitHub Project",
            "channel": {"handle": "dev1"},
            "origin": {"platform": "github", "external_id": "dev1/fast", "links": []},
        })

        printables_gate = Event()
        printables_started = Event()
        github_done = Event()

        mock_printables = MagicMock(spec=PrintablesAcquirer)
        def blocking_acquire(rel: Release, output_dir: Path, **kwargs: object) -> PrintablesAcquisition:
            printables_started.set()
            printables_gate.wait(5)
            dest = output_dir / "p.zip"
            dest.write_bytes(b"P" * 10)
            return PrintablesAcquisition(dest, "https://example.com/p.zip")

        mock_printables.acquire.side_effect = blocking_acquire

        mock_github = MagicMock(spec=GitHubAcquirer)
        def fast_gh_acquire(rel: Release, output_dir: Path, **kwargs: object) -> GitHubAcquisition:
            dest = output_dir / "repo.zip"
            dest.write_bytes(b"G" * 20)
            github_done.set()
            return GitHubAcquisition(dest, "https://github.com/dev1/fast/zipball")

        mock_github.acquire.side_effect = fast_gh_acquire

        pipeline_settings = Settings(
            endpoint=self.settings.endpoint,
            data_dir=self.root,
            min_free_space=0,
            printables_concurrency=2,
            github_concurrency=2,
            torrent_piece_length=16 * 1024,
        )
        pipeline = MirrorPipeline(
            pipeline_settings,
            MagicMock(),
            self.lbry_acquirer,
            self.store,
            self.publisher,
            printables_acquirer=mock_printables,
            github_acquirer=mock_github,
        )

        class MultiIndex:
            def releases(self, **kwargs: object):
                return iter([p1, p2, gh])

        pipeline.index_client = MultiIndex()

        with ThreadPoolExecutor(max_workers=1) as executor:
            cycle = executor.submit(pipeline.run_cycle)
            try:
                # Wait for printables to start
                self.assertTrue(printables_started.wait(3))
                # GitHub must complete even while Printables is blocked on its gate!
                self.assertTrue(github_done.wait(3))
            finally:
                printables_gate.set()
            res = cycle.result(timeout=10)

        self.assertEqual(res.ready, 3)
        gh_job = self.store.get(gh.id, gh.sd_hash)
        self.assertEqual(gh_job.state, JobState.AWAITING_INDEX)
