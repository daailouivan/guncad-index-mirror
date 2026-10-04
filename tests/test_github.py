from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from guncadmirror.cancellation import AcquisitionCancelled
from guncadmirror.github import GitHubAcquirer
from guncadmirror.models import Release

from .helpers import FakeResponse, QueueSession


def make_github_release(
    *,
    release_id: str = "github-owner-repo-v1.0",
    name: str = "Test CAD Release",
    url: str = "https://github.com/owner/repo/releases/tag/v1.0",
    external_id: str = "owner/repo/v1.0",
) -> Release:
    payload = {
        "id": release_id,
        "name": name,
        "channel": {"handle": "owner"},
        "origin": {
            "platform": "github",
            "external_id": external_id,
            "size": 2048,
            "popularity": 1.0,
            "links": [{"name": "GitHub", "url": url}],
        },
    }
    return Release.from_api(payload)


class GitHubAcquirerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("guncadmirror.github.download_url_to_file")
    def test_acquires_release_asset(self, mock_download: object) -> None:
        release_meta = {
            "assets": [
                {
                    "name": "cad_package.zip",
                    "size": 5000,
                    "browser_download_url": "https://github.com/owner/repo/releases/download/v1.0/cad_package.zip",
                }
            ]
        }
        session = QueueSession(FakeResponse(release_meta))
        acquirer = GitHubAcquirer(session=session)  # type: ignore[arg-type]
        release = make_github_release()

        acquisition = acquirer.acquire(release, self.output_dir)

        self.assertEqual(acquisition.path, self.output_dir / "cad_package.zip")
        self.assertEqual(
            acquisition.source_url,
            "https://github.com/owner/repo/releases/download/v1.0/cad_package.zip",
        )

    @patch("guncadmirror.github.download_url_to_file")
    def test_falls_back_to_zipball(self, mock_download: object) -> None:
        release_meta = {
            "assets": [],
            "zipball_url": "https://api.github.com/repos/owner/repo/zipball/v1.0",
        }
        session = QueueSession(FakeResponse(release_meta))
        acquirer = GitHubAcquirer(session=session)  # type: ignore[arg-type]
        release = make_github_release()

        acquisition = acquirer.acquire(release, self.output_dir)

        self.assertEqual(acquisition.path, self.output_dir / "repo-v1.0.zip")
        self.assertEqual(
            acquisition.source_url,
            "https://api.github.com/repos/owner/repo/zipball/v1.0",
        )

    def test_respects_cancellation(self) -> None:
        stop = Event()
        stop.set()
        acquirer = GitHubAcquirer()
        release = make_github_release()

        with self.assertRaises(AcquisitionCancelled):
            acquirer.acquire(release, self.output_dir, stop=stop)
