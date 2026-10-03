from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event

from guncadmirror.cancellation import AcquisitionCancelled
from guncadmirror.http_download import DownloadError, download_url_to_file

from .helpers import FakeResponse, QueueSession


class HttpDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dest_path = Path(self.temp_dir.name) / "downloaded.bin"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_downloads_file_successfully_and_reports_progress(self) -> None:
        content = b"sample content " * 100
        progress_calls: list[tuple[int, int | None]] = []

        class MockResponse:
            headers = {"Content-Length": str(len(content))}

            def __enter__(self) -> MockResponse:
                return self

            def __exit__(self, *args: object) -> None:
                pass

            def raise_for_status(self) -> None:
                pass

            def iter_content(self, chunk_size: int = 1024) -> list[bytes]:
                return [content[:500], content[500:]]

        class MockSession:
            def get(self, *args: object, **kwargs: object) -> MockResponse:
                return MockResponse()

        def progress(done: int, total: int | None) -> None:
            progress_calls.append((done, total))

        bytes_written = download_url_to_file(
            "https://example.com/file.bin",
            self.dest_path,
            session=MockSession(),  # type: ignore[arg-type]
            progress=progress,
        )

        self.assertEqual(bytes_written, len(content))
        self.assertEqual(self.dest_path.read_bytes(), content)
        self.assertEqual(progress_calls[-1], (len(content), len(content)))

    def test_respects_cancellation_during_download(self) -> None:
        stop = Event()
        stop.set()

        with self.assertRaises(AcquisitionCancelled):
            download_url_to_file(
                "https://example.com/file.bin",
                self.dest_path,
                stop=stop,
            )

        self.assertFalse(self.dest_path.exists())

    def test_retries_on_network_errors_and_fails_after_limit(self) -> None:
        import requests

        class FailingSession:
            def get(self, *args: object, **kwargs: object) -> None:
                raise requests.RequestException("connection reset")

        with self.assertRaises(DownloadError):
            download_url_to_file(
                "https://example.com/file.bin",
                self.dest_path,
                session=FailingSession(),  # type: ignore[arg-type]
                attempts=2,
                backoff=0.01,
            )
