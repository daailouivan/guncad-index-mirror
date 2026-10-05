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
        acquirer = PrintablesAcquirer(session=session, pacing=0)  # type: ignore[arg-type]
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
        acquirer = PrintablesAcquirer(session=session, pacing=0)  # type: ignore[arg-type]
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
        acquirer = PrintablesAcquirer(session=session, pacing=0)  # type: ignore[arg-type]
        release = make_printables_release()

        with self.assertRaises(PrintablesProtocolError):
            acquirer.acquire(release, self.output_dir)

    def test_respects_cancellation(self) -> None:
        stop = Event()
        stop.set()
        acquirer = PrintablesAcquirer(pacing=0)
        release = make_printables_release()

        with self.assertRaises(AcquisitionCancelled):
            acquirer.acquire(release, self.output_dir, stop=stop)

    @patch("guncadmirror.printables.download_url_to_file")
    def test_handles_429_rate_limit_with_retry_after(
        self, mock_download: object
    ) -> None:
        import requests

        sleep_calls: list[float] = []

        # First request to ModelFiles returns 429 with Retry-After: 3
        resp_429 = FakeResponse(
            status_code=429,
            headers={"Retry-After": "3"},
            status_error=requests.HTTPError(
                "429 Client Error",
                response=FakeResponse(status_code=429, headers={"Retry-After": "3"}),  # type: ignore[arg-type]
            ),
        )
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
            resp_429,
            FakeResponse(files_response),
            FakeResponse(link_response),
        )
        acquirer = PrintablesAcquirer(
            session=session,  # type: ignore[arg-type]
            pacing=0,
            sleep=lambda d: sleep_calls.append(d),
        )
        release = make_printables_release()

        acquisition = acquirer.acquire(release, self.output_dir)
        self.assertEqual(acquisition.path, self.output_dir / "box.3mf")
        # Should have slept with delay >= 3.0s from Retry-After
        self.assertTrue(any(d >= 3.0 for d in sleep_calls))

    def test_respects_pacing_between_graphql_requests(self) -> None:
        sleep_calls: list[float] = []
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
        acquirer = PrintablesAcquirer(
            session=session,  # type: ignore[arg-type]
            pacing=0.5,
            sleep=lambda d: sleep_calls.append(d),
        )
        release = make_printables_release()

        with self.assertRaises(PrintablesProtocolError):
            acquirer.acquire(release, self.output_dir)

        self.assertIn(0.5, sleep_calls)

    @patch("guncadmirror.printables.download_url_to_file")
    def test_handles_429_rate_limit_without_retry_after_defaulting_to_60s(
        self, mock_download: object
    ) -> None:
        import requests

        sleep_calls: list[float] = []

        # 429 response without Retry-After header
        resp_429 = FakeResponse(
            status_code=429,
            headers={},
            status_error=requests.HTTPError(
                "429 Client Error",
                response=FakeResponse(status_code=429, headers={}),  # type: ignore[arg-type]
            ),
        )
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
            resp_429,
            FakeResponse(files_response),
            FakeResponse(link_response),
        )
        acquirer = PrintablesAcquirer(
            session=session,  # type: ignore[arg-type]
            pacing=0,
            sleep=lambda d: sleep_calls.append(d),
        )
        release = make_printables_release()

        acquisition = acquirer.acquire(release, self.output_dir)
        self.assertEqual(acquisition.path, self.output_dir / "box.3mf")
        # Without Retry-After, cooldown must be at least 60s
        self.assertTrue(any(d >= 60.0 for d in sleep_calls))

    @patch("guncadmirror.printables.download_url_to_file")
    def test_deduplicates_conflicting_filenames_in_multi_file_model(
        self, mock_download: object
    ) -> None:
        def fake_download(url: str, dest: Path, **kwargs: object) -> int:
            dest.write_bytes(b"content for " + dest.name.encode())
            return 100

        mock_download.side_effect = fake_download  # type: ignore[attr-defined]

        # Model with two files having identical names
        files_response = {
            "data": {
                "model": {
                    "id": "1863745",
                    "name": "MultiPart",
                    "stls": [
                        {"id": "1", "name": "part.stl", "fileSize": 200},
                        {"id": "2", "name": "part.stl", "fileSize": 300},
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
                    "output": {"link": "https://files.printables.com/part1.stl"},
                }
            }
        }
        link_response2 = {
            "data": {
                "getDownloadLink": {
                    "ok": True,
                    "output": {"link": "https://files.printables.com/part2.stl"},
                }
            }
        }
        session = QueueSession(
            FakeResponse(files_response),
            FakeResponse(link_response1),
            FakeResponse(link_response2),
        )
        acquirer = PrintablesAcquirer(session=session, pacing=0)  # type: ignore[arg-type]
        release = make_printables_release()

        acquisition = acquirer.acquire(release, self.output_dir)

        self.assertTrue(acquisition.path.is_file())
        with zipfile.ZipFile(acquisition.path, "r") as zf:
            namelist = sorted(zf.namelist())
            self.assertEqual(namelist, ["part.stl", "part_1.stl"])

    def test_concurrent_threads_scheduled_pacing(self) -> None:
        import concurrent.futures

        sleep_calls: list[float] = []
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
        # Multi-thread test making 3 calls with pacing=0.5
        session = QueueSession(
            FakeResponse(files_response),
            FakeResponse(files_response),
            FakeResponse(files_response),
        )
        acquirer = PrintablesAcquirer(
            session=session,  # type: ignore[arg-type]
            pacing=0.5,
            sleep=lambda d: sleep_calls.append(d),
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(acquirer._graphql_post, "query {}", {}, f"Op{i}")
                for i in range(3)
            ]
            for f in futures:
                f.result()

        # Each request must have waited a scheduled slot
        self.assertEqual(len(sleep_calls), 3)
        self.assertTrue(all(d >= 0.0 for d in sleep_calls))

    def test_429_cooldown_pauses_subsequent_requests(self) -> None:
        import requests

        sleep_calls: list[float] = []
        resp_429 = FakeResponse(
            status_code=429,
            headers={},
            status_error=requests.HTTPError(
                "429 Client Error",
                response=FakeResponse(status_code=429, headers={}),  # type: ignore[arg-type]
            ),
        )
        resp_ok = FakeResponse({"data": {"ok": True}})
        session = QueueSession(
            resp_429,  # Thread 1 attempt 1 fails with 429
            resp_ok,   # Thread 1 attempt 2 succeeds
            resp_ok,   # Request 2 succeeds
        )
        acquirer = PrintablesAcquirer(
            session=session,  # type: ignore[arg-type]
            pacing=0,
            sleep=lambda d: sleep_calls.append(d),
        )

        res1 = acquirer._graphql_post("query {}", {}, "Op1")
        self.assertEqual(res1, {"ok": True})
        # 60s cooldown was invoked
        self.assertTrue(any(d >= 60.0 for d in sleep_calls))

