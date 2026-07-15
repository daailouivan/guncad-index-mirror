from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from guncadmirror.torrent import TorrentError, bencode, create_torrent


class TorrentTests(unittest.TestCase):
    def test_bencode_canonical_types_and_dictionary_order(self):
        self.assertEqual(bencode(b"spam"), b"4:spam")
        self.assertEqual(bencode("é"), b"2:\xc3\xa9")
        self.assertEqual(bencode(42), b"i42e")
        self.assertEqual(bencode([b"a", 2]), b"l1:ai2ee")
        self.assertEqual(bencode({b"z": 1, "a": "x"}), b"d1:a1:x1:zi1ee")
        for invalid in (True, None, (1, 2)):
            with self.subTest(invalid=invalid), self.assertRaises(TorrentError):
                bencode(invalid)
        with self.assertRaisesRegex(TorrentError, "keys must"):
            bencode({1: "invalid"})
        with self.assertRaisesRegex(TorrentError, "duplicate"):
            bencode({b"same": 1, "same": 2})

    def test_creates_stable_v1_torrent_and_magnet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload file.bin"
            payload.write_bytes(b"a" * 16384 + b"tail")
            destination = root / "out" / "payload.torrent"
            trackers = (
                "udp://tracker.test:80/announce",
                "https://tracker2.test/announce",
            )

            artifact = create_torrent(
                payload, destination, piece_length=16384, trackers=trackers
            )
            first_bytes = destination.read_bytes()
            second = create_torrent(
                payload, destination, piece_length=16384, trackers=trackers
            )

            piece_hashes = (
                hashlib.sha1(b"a" * 16384, usedforsecurity=False).digest()
                + hashlib.sha1(b"tail", usedforsecurity=False).digest()
            )
            info = {
                b"length": 16388,
                b"name": "payload file.bin",
                b"piece length": 16384,
                b"pieces": piece_hashes,
            }
            self.assertEqual(
                artifact.info_hash,
                hashlib.sha1(bencode(info), usedforsecurity=False).hexdigest(),
            )
            self.assertEqual(artifact.piece_count, 2)
            self.assertEqual(
                artifact.torrent_sha256, hashlib.sha256(first_bytes).hexdigest()
            )
            self.assertEqual(first_bytes, destination.read_bytes())
            self.assertEqual(artifact, second)
            query = parse_qs(urlsplit(artifact.magnet_uri).query)
            self.assertEqual(query["xt"], [f"urn:btih:{artifact.info_hash}"])
            self.assertEqual(query["dn"], ["payload file.bin"])
            self.assertEqual(query["tr"], list(trackers))

    def test_rejects_missing_empty_and_invalid_piece_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing"
            with self.assertRaisesRegex(TorrentError, "does not exist"):
                create_torrent(missing, root / "x.torrent")

            empty = root / "empty"
            empty.touch()
            with self.assertRaisesRegex(TorrentError, "empty"):
                create_torrent(empty, root / "x.torrent", piece_length=16384)

            empty.write_bytes(b"x")
            for piece_length in (1, 20000):
                with (
                    self.subTest(piece_length=piece_length),
                    self.assertRaisesRegex(TorrentError, "piece length"),
                ):
                    create_torrent(empty, root / "x.torrent", piece_length=piece_length)
