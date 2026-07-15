from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from guncadmirror.models import FileHashes, TorrentArtifact
from guncadmirror.publisher import OutboxPublisher

from .helpers import make_release


class OutboxPublisherTests(unittest.TestCase):
    def test_writes_complete_stable_publication_manifest(self) -> None:
        release = make_release()
        hashes = FileHashes(size=7, sha384="c" * 96, sha256="d" * 64)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file_path = root / "payload.zip"
            file_path.write_bytes(b"payload")
            torrent_path = root / "outbox" / "payload.torrent"
            torrent_path.parent.mkdir()
            torrent_path.write_bytes(b"torrent")
            torrent = TorrentArtifact(
                file_path=file_path,
                torrent_path=torrent_path,
                piece_length=1024**2,
                piece_count=1,
                info_hash="e" * 40,
                torrent_sha256="f" * 64,
                magnet_uri="magnet:?xt=urn:btih:" + "e" * 40,
                trackers=("udp://tracker.example:80",),
            )

            bundle = OutboxPublisher(root / "outbox").publish(release, hashes, torrent)
            document = json.loads(bundle.manifest_path.read_text())

            self.assertEqual(
                bundle.manifest_path,
                root / "outbox" / release.id / release.sd_hash / "manifest.json",
            )

        self.assertEqual(bundle.release, release)
        self.assertEqual(document["schema"], "guncad-mirror-publication-v1")
        self.assertEqual(document["status"], "awaiting-index")
        self.assertEqual(document["lbry"]["sd_hash"], release.sd_hash)
        self.assertEqual(document["artifact"]["sha384"], hashes.sha384)
        self.assertEqual(document["torrent"]["btih"], torrent.info_hash)
        self.assertEqual(document["torrent"]["trackers"], list(torrent.trackers))


if __name__ == "__main__":
    unittest.main()
