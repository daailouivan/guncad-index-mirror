from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from threading import Event
from unittest.mock import patch

from guncadmirror.cancellation import AcquisitionCancelled
from guncadmirror.models import Release
from guncadmirror.printables import (
    PrintablesAcquirer,
    PrintablesProtocolError,
)

from .helpers import FakeResponse, QueueSession


def make_printables_release(
    *,
    release_id: str = "printables-1863745",
    name: str = "AmmoBox 22LR",
    external_id: str = "1863745",
    url: str = "https://printables.com/model/1863745-ammobox-22lr",
) -> Release:
    payload = {
        "id": release_id,
        "name": name,
        "channel": {"handle": "Author_123"},
        "origin": {
            "platform": "printables",
            "external_id": external_id,
            "size": 1024,
            "popularity": 1.0,
            "links": [{"name": "Printables", "url": url}],
        },
    }
    return Release.from_api(payload)


class PrintablesAcquirerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("guncadmirror.printables.download_url_to_file")
    def test_acquires_single_file_model(self, mock_download: object) -> None:
        files_response = {
            "data": {
                "model": {
                    "id": "1863745",
                    "name": "AmmoBox 22LR",
                    "stls": [{"id": "7787145", "name": "box.3mf", "fileSize": 500}],
                    "gcodes": [],
                    "slas": [],
                    "otherFiles": [],
                }
            }
        }
        link_response = {
            "data": {
                "getDownloadLink": {
                    "ok": True,
                    "output": {"link": "https://files.printables.com/box.3mf"},
                }
            }
        }
        session = QueueSession(
            FakeResponse(files_response), FakeResponse(link_response)
        )
        acquirer = PrintablesAcquirer(session=session)  # type: ignore[arg-type]
        release = make_printables_release()

        acquisition = acquirer.acquire(release, self.output_dir)

        self.assertEqual(acquisition.path, self.output_dir / "box.3mf")
        self.assertEqual(acquisition.source_url, "https://files.printables.com/box.3mf")

    @patch("guncadmirror.printables.download_url_to_file")
    def test_acquires_multi_file_model_and_packages_zip(
        self, mock_download: object
    ) -> None:
        def fake_download(url: str, dest: Path, **kwargs: object) -> int:
            dest.write_bytes(b"content for " + dest.name.encode())
            return 100

        mock_download.side_effect = fake_download  # type: ignore[attr-defined]

        files_response = {
            "data": {
                "model": {
                    "id": "1863745",
                    "name": "AmmoBox 22LR",
                    "stls": [
                        {"id": "1", "name": "lid.stl", "fileSize": 200},
                        {"id": "2", "name": "base.stl", "fileSize": 300},
                    ],
                    "gcodes": [],
                    "slas": [],
                    "otherFiles": [],
                }
            }
        }
        link_response1 = {
            "data": {
                "getDownloadLink": {
                    "ok": True,
                    "output": {"link": "https://files.printables.com/lid.stl"},
                }
            }
        }
        link_response2 = {
            "data": {
                "getDownloadLink": {
                    "ok": True,
                    "output": {"link": "https://files.printables.com/base.stl"},
                }
            }
        }
        session = QueueSession(
            FakeResponse(files_response),
            FakeResponse(link_response1),
            FakeResponse(link_response2),
        )
        acquirer = PrintablesAcquirer(session=session)  # type: ignore[arg-type]
        release = make_printables_release()

        acquisition = acquirer.acquire(release, self.output_dir)

        self.assertTrue(acquisition.path.name.endswith(".zip"))
        self.assertTrue(acquisition.path.is_file())
        with zipfile.ZipFile(acquisition.path, "r") as zf:
            namelist = sorted(zf.namelist())
            self.assertEqual(namelist, ["base.stl", "lid.stl"])

    def test_raises_protocol_error_on_empty_model(self) -> None:
        files_response = {
            "data": {
                "model": {
                    "id": "1863745",
                    "name": "Empty Model",
                    "stls": [],
                    "gcodes": [],
                    "slas": [],
                    "otherFiles": [],
                }
            }
        }
        session = QueueSession(FakeResponse(files_response))
        acquirer = PrintablesAcquirer(session=session)  # type: ignore[arg-type]
        release = make_printables_release()

        with self.assertRaises(PrintablesProtocolError):
            acquirer.acquire(release, self.output_dir)

    def test_respects_cancellation(self) -> None:
        stop = Event()
        stop.set()
        acquirer = PrintablesAcquirer()
        release = make_printables_release()

        with self.assertRaises(AcquisitionCancelled):
            acquirer.acquire(release, self.output_dir, stop=stop)
