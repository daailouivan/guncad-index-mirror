from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

from guncadmirror.cancellation import AcquisitionCancelled
from guncadmirror.http_acquirer import (
    HttpAcquirer,
    HttpError,
    parse_content_disposition_filename,
    parse_url_filename,
)
from guncadmirror.models import Release

from .helpers import FakeResponse


def make_http_release(
    *,
    release_id: str = "http-release-123",
    name: str = "Test Direct CAD",
    url: str = "https://example.com/downloads/model.zip",
    links: list[dict[str, object]] | None = None,
) -> Release:
    if links is None:
        links = [{"name": "Direct Download", "url": url, "download": True}]
    payload = {
        "id": release_id,
        "name": name,
        "channel": {"handle": "cad-designer"},
        "origin": {
            "platform": "http",
            "external_id": release_id,
            "size": 4096,
            "popularity": 1.0,
            "links": links,
        },
    }
    return Release.from_api(payload)


class HttpAcquirerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_content_disposition_filename(self) -> None:
        self.assertIsNone(parse_content_disposition_filename(None))
        self.assertIsNone(parse_content_disposition_filename(""))
        self.assertEqual(
            parse_content_disposition_filename(
                'attachment; filename="cad_package.zip"'
            ),
            "cad_package.zip",
        )
        self.assertEqual(
            parse_content_disposition_filename("attachment; filename=model.stl"),
            "model.stl",
        )
        self.assertEqual(
            parse_content_disposition_filename(
                "attachment; filename*=UTF-8''my%20cool%20part.step"
            ),
            "my cool part.step",
        )

    def test_parse_url_filename(self) -> None:
        self.assertEqual(
            parse_url_filename("https://example.com/files/part.stl"),
            "part.stl",
        )
        self.assertEqual(
            parse_url_filename("https://example.com/files/archive.tar.gz"),
            "archive.tar.gz",
        )
        self.assertEqual(
            parse_url_filename(
                "https://example.com/download?filename=custom_model.zip"
            ),
            "custom_model.zip",
        )
        self.assertIsNone(parse_url_filename("https://example.com/download?id=12345"))

    @patch("guncadmirror.http_acquirer.download_url_to_file")
    def test_acquires_direct_url_with_path_filename(
        self, mock_download: MagicMock
    ) -> None:
        release = make_http_release(
            url="https://example.com/files/lower-receiver.zip",
            links=[
                {
                    "name": "Download",
                    "url": "https://example.com/files/lower-receiver.zip",
                }
            ],
        )
        acquirer = HttpAcquirer()
        acquisition = acquirer.acquire(release, self.output_dir)

        self.assertEqual(acquisition.path, self.output_dir / "lower-receiver.zip")
        self.assertEqual(
            acquisition.source_url, "https://example.com/files/lower-receiver.zip"
        )
        mock_download.assert_called_once()

    @patch("guncadmirror.http_acquirer.download_url_to_file")
    def test_acquires_url_resolving_filename_from_content_disposition(
        self, mock_download: MagicMock
    ) -> None:
        class HeadSession:
            def head(self, url: str, **kwargs: object) -> FakeResponse:
                resp = FakeResponse(status_code=200)
                resp.headers = {
                    "Content-Disposition": 'attachment; filename="resolved_part.stl"'
                }  # type: ignore[attr-defined]
                resp.url = url  # type: ignore[attr-defined]
                return resp

        release = make_http_release(
            url="https://example.com/api/get_model?id=99",
            links=[{"name": "API", "url": "https://example.com/api/get_model?id=99"}],
        )
        acquirer = HttpAcquirer(session=HeadSession())  # type: ignore[arg-type]
        acquisition = acquirer.acquire(release, self.output_dir)

        self.assertEqual(acquisition.path, self.output_dir / "resolved_part.stl")
        self.assertEqual(
            acquisition.source_url, "https://example.com/api/get_model?id=99"
        )

    @patch("guncadmirror.http_acquirer.download_url_to_file")
    def test_falls_back_to_release_name_zip_when_probing_fails(
        self, mock_download: MagicMock
    ) -> None:
        class FailingHeadSession:
            def head(self, url: str, **kwargs: object) -> FakeResponse:
                raise ConnectionResetError("network error during head probe")

        release = make_http_release(
            name="Glock 19 Frame",
            url="https://example.com/api/download/glock",
            links=[
                {"name": "Download", "url": "https://example.com/api/download/glock"}
            ],
        )
        acquirer = HttpAcquirer(session=FailingHeadSession())  # type: ignore[arg-type]
        acquisition = acquirer.acquire(release, self.output_dir)

        self.assertEqual(acquisition.path, self.output_dir / "Glock-19-Frame.zip")
        self.assertEqual(
            acquisition.source_url, "https://example.com/api/download/glock"
        )

    def test_raises_when_no_valid_http_link(self) -> None:
        release = make_http_release(
            links=[{"name": "FTP", "url": "ftp://example.com/file.zip"}],
        )
        # remove fallback url
        object.__setattr__(release, "url", None)
        acquirer = HttpAcquirer()

        with self.assertRaises(HttpError):
            acquirer.acquire(release, self.output_dir)

    def test_respects_cancellation(self) -> None:
        stop = Event()
        stop.set()
        release = make_http_release()
        acquirer = HttpAcquirer()

        with self.assertRaises(AcquisitionCancelled):
            acquirer.acquire(release, self.output_dir, stop=stop)
