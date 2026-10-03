from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from guncadmirror.github import GitHubAcquirer, GitHubAcquisition
from guncadmirror.models import AcquisitionTransport, JobState, Release
from guncadmirror.pipeline import MirrorPipeline
from guncadmirror.printables import PrintablesAcquirer, PrintablesAcquisition
from guncadmirror.publisher import OutboxPublisher
from guncadmirror.settings import Settings
from guncadmirror.state import JobStore


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
        def fake_acquire(rel: Release, output_dir: Path, **kwargs: object) -> PrintablesAcquisition:
            dest = output_dir / "22LR_AmmoBox.3mf"
            dest.write_bytes(b"A" * 50)
            return PrintablesAcquisition(path=dest, source_url="https://files.printables.com/file.3mf")

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
        self.assertEqual(manifest["acquisition"]["transport"], AcquisitionTransport.PRINTABLES)
        self.assertEqual(manifest["acquisition"]["source_url"], "https://files.printables.com/file.3mf")
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
        def fake_acquire(rel: Release, output_dir: Path, **kwargs: object) -> GitHubAcquisition:
            dest = output_dir / "coolpart.zip"
            dest.write_bytes(b"B" * 60)
            return GitHubAcquisition(path=dest, source_url="https://github.com/org/coolpart/asset.zip")

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
        self.assertEqual(manifest["acquisition"]["transport"], AcquisitionTransport.GITHUB)
