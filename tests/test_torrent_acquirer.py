from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

from guncadmirror.cancellation import AcquisitionCancelled
from guncadmirror.models import Release
from guncadmirror.qbittorrent import QBitClient, QBitTorrent
from guncadmirror.settings import Settings
from guncadmirror.torrent import bencode
from guncadmirror.torrent_acquirer import (
    TorrentAcquirer,
    TorrentAcquisitionTimeout,
    TorrentAcquisitionUnavailable,
    extract_info_hash_from_magnet,
)

from .helpers import FakeResponse


def make_torrent_release(
    *,
    release_id: str = "torrent-release-123",
    name: str = "Test Torrent CAD",
    url: str | None = None,
    links: list[dict[str, object]] | None = None,
) -> Release:
    if links is None:
        magnet = "magnet:?xt=urn:btih:ff54e65a94386a8375b836a294dc9333e0c393cb&dn=payload.zip"
        links = [{"name": "Magnet", "url": magnet}]
    payload = {
        "id": release_id,
        "name": name,
        "channel": {"handle": "cad-group"},
        "origin": {
            "platform": "torrent",
            "external_id": release_id,
            "size": 1024,
            "popularity": 1.0,
            "links": links,
        },
    }
    return Release.from_api(payload)


class TorrentAcquirerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.output_dir = self.root / "releases" / "test-release"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.settings = Settings(
            data_dir=self.root,
            qbittorrent_data_dir=Path("/downloads"),
            torrent_download_timeout=10,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_extract_info_hash_from_magnet(self) -> None:
        # Standard 40-character hex
        hex_magnet = (
            "magnet:?xt=urn:btih:ff54e65a94386a8375b836a294dc9333e0c393cb&dn=test.zip"
        )
        self.assertEqual(
            extract_info_hash_from_magnet(hex_magnet),
            "ff54e65a94386a8375b836a294dc9333e0c393cb",
        )

        # 32-character Base32
        # Base32: 65KK4WSUG43I65NYG2RJJXE3G4====== (which decodes to 20 bytes hex)
        # Let's test a known roundtrip
        import base64

        hex_expected = "0123456789abcdef0123456789abcdef01234567"
        b32_str = base64.b32encode(bytes.fromhex(hex_expected)).decode("ascii")
        b32_magnet = f"magnet:?xt=urn:btih:{b32_str}&dn=sample.zip"
        self.assertEqual(extract_info_hash_from_magnet(b32_magnet), hex_expected)

        # Invalid magnet
        self.assertIsNone(extract_info_hash_from_magnet("magnet:?dn=test.zip"))
        self.assertIsNone(extract_info_hash_from_magnet("https://example.com/file.zip"))

    def test_raises_when_client_is_none(self) -> None:
        acquirer = TorrentAcquirer(self.settings, client=None)
        release = make_torrent_release()

        with self.assertRaises(TorrentAcquisitionUnavailable):
            acquirer.acquire(release, self.output_dir)

    def test_acquires_magnet_link_successfully(self) -> None:
        client = MagicMock(spec=QBitClient)
        info_hash = "ff54e65a94386a8375b836a294dc9333e0c393cb"

        # Mock download progress
        def fake_torrent(h: str) -> QBitTorrent:
            # Simulate downloaded file appearing
            payload_file = self.output_dir / "downloaded_model.stl"
            payload_file.write_bytes(b"STL CONTENT" * 10)
            return QBitTorrent(
                info_hash=h,
                content_path=str(payload_file),
                save_path=str(self.output_dir),
                progress=1.0,
                amount_left=0,
                total_size=110,
                state="uploading",
                force_start=True,
                category="guncad-intake",
                tags=("guncad-intake",),
            )

        client.torrent.side_effect = fake_torrent

        acquirer = TorrentAcquirer(self.settings, client=client, poll_interval=0.01)
        release = make_torrent_release()

        acquisition = acquirer.acquire(release, self.output_dir)

        client.add_url.assert_called_once()
        client.force_start.assert_called_once_with(info_hash)
        client.reannounce.assert_called_once_with(info_hash)
        client.delete.assert_called_once_with(info_hash, delete_files=False)

        self.assertEqual(acquisition.path, self.output_dir / "downloaded_model.stl")
        self.assertTrue(acquisition.path.is_file())

    def test_acquires_torrent_file_and_packages_directory(self) -> None:
        # Create a valid single-file torrent bytes to mock the HTTP download
        raw_torrent = bencode(
            {
                b"announce": b"udp://tracker:80",
                b"info": {
                    b"name": b"model_folder",
                    b"length": 100,
                    b"piece length": 16 * 1024,
                    b"pieces": b"a" * 20,
                },
            }
        )

        class TorrentSession:
            def get(self, url: str, **kwargs: object) -> FakeResponse:
                resp = FakeResponse(status_code=200)
                resp.content = raw_torrent  # type: ignore[attr-defined]
                return resp

        client = MagicMock(spec=QBitClient)

        def fake_torrent(h: str) -> QBitTorrent:
            # Simulate multi-file directory download
            sub_dir = self.output_dir / "model_folder"
            sub_dir.mkdir(parents=True, exist_ok=True)
            (sub_dir / "part1.stl").write_bytes(b"PART1")
            (sub_dir / "part2.stl").write_bytes(b"PART2")
            return QBitTorrent(
                info_hash=h,
                content_path=str(sub_dir),
                save_path=str(self.output_dir),
                progress=1.0,
                amount_left=0,
                total_size=10,
                state="uploading",
                force_start=True,
                category="guncad-intake",
                tags=("guncad-intake",),
            )

        client.torrent.side_effect = fake_torrent

        acquirer = TorrentAcquirer(
            self.settings,
            client=client,
            session=TorrentSession(),  # type: ignore[arg-type]
            poll_interval=0.01,
        )
        release = make_torrent_release(
            links=[
                {"name": "Torrent", "url": "https://example.com/files/model.torrent"}
            ],
        )

        acquisition = acquirer.acquire(release, self.output_dir)

        client.add.assert_called_once()
        client.delete.assert_called_once()

        self.assertTrue(acquisition.path.is_file())
        self.assertTrue(acquisition.path.name.endswith(".zip"))

    def test_times_out_when_not_completing(self) -> None:
        client = MagicMock(spec=QBitClient)
        # Always 50% progress
        client.torrent.return_value = QBitTorrent(
            info_hash="ff54e65a94386a8375b836a294dc9333e0c393cb",
            content_path=str(self.output_dir),
            save_path=str(self.output_dir),
            progress=0.5,
            amount_left=500,
            total_size=1000,
            state="downloading",
            force_start=True,
            category="guncad-intake",
            tags=("guncad-intake",),
        )

        acquirer = TorrentAcquirer(
            self.settings,
            client=client,
            download_timeout=0.05,
            poll_interval=0.01,
        )
        release = make_torrent_release()

        with self.assertRaises(TorrentAcquisitionTimeout):
            acquirer.acquire(release, self.output_dir)

        client.delete.assert_called_once()

    def test_respects_cancellation(self) -> None:
        stop = Event()
        stop.set()
        client = MagicMock(spec=QBitClient)
        acquirer = TorrentAcquirer(self.settings, client=client)
        release = make_torrent_release()

        with self.assertRaises(AcquisitionCancelled):
            acquirer.acquire(release, self.output_dir, stop=stop)
