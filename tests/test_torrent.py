from __future__ import annotations

import hashlib
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from guncadmirror import torrent as torrent_module
from guncadmirror.cancellation import AcquisitionCancelled
from guncadmirror.torrent import (
    TorrentError,
    bencode,
    create_torrent,
    parse_torrent,
    strip_torrent_trackers,
)


def torrent_bytes(
    *,
    name: str = "payload.zip",
    length: int = 100,
    piece_length: int = 16 * 1024,
    announce: str | None = "https://tracker.example/announce",
    announce_list: list[list[str]] | None = None,
) -> bytes:
    piece_count = math.ceil(length / piece_length)
    document = {
        b"info": {
            b"length": length,
            b"name": name.encode(),
            b"piece length": piece_length,
            b"pieces": b"p" * 20 * piece_count,
        }
    }
    if announce is not None:
        document[b"announce"] = announce
    if announce_list is not None:
        document[b"announce-list"] = announce_list
    return bencode(document)


class TorrentTests(unittest.TestCase):
    def assert_invalid(self, raw: bytes) -> None:
        with self.assertRaises(TorrentError):
            parse_torrent(raw)

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
            progress: list[int] = []

            artifact = create_torrent(
                payload,
                destination,
                piece_length=16384,
                progress=progress.append,
            )
            first_bytes = destination.read_bytes()
            second = create_torrent(payload, destination, piece_length=16384)

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
            self.assertNotIn("tr", query)
            self.assertEqual(progress, [0, 16384, 16388])

            parsed = parse_torrent(first_bytes)
            self.assertEqual(parsed.info_hash, artifact.info_hash)
            self.assertEqual(parsed.torrent_sha256, artifact.torrent_sha256)
            self.assertEqual(parsed.file_name, payload.name)
            self.assertEqual(parsed.file_length, payload.stat().st_size)
            self.assertEqual(parsed.piece_length, artifact.piece_length)
            self.assertEqual(parsed.piece_count, artifact.piece_count)
            self.assertEqual(parsed.trackers, ())
            self.assertEqual(parsed.magnet_uri, artifact.magnet_uri)

    def test_strips_trackers_without_changing_info_or_other_top_level_data(self):
        raw = torrent_bytes(
            announce="https://tracker.example/announce",
            announce_list=[
                ["https://tracker.example/announce"],
                ["udp://tracker.example:80/announce"],
            ],
        )
        root = torrent_module._BencodeDecoder(raw)
        root.decode()
        self.assertIsNotNone(root.info_span)
        info_start, info_end = root.info_span

        stripped = strip_torrent_trackers(raw)
        stripped_root = torrent_module._BencodeDecoder(stripped)
        document = stripped_root.decode()
        self.assertIsNotNone(stripped_root.info_span)
        stripped_start, stripped_end = stripped_root.info_span

        self.assertNotIn(b"announce", document)
        self.assertNotIn(b"announce-list", document)
        self.assertEqual(
            raw[info_start:info_end],
            stripped[stripped_start:stripped_end],
        )
        self.assertEqual(
            parse_torrent(raw).info_hash, parse_torrent(stripped).info_hash
        )
        self.assertEqual(strip_torrent_trackers(stripped), stripped)

        with self.assertRaisesRegex(TorrentError, "info dictionary"):
            strip_torrent_trackers(bencode({b"announce": b"https://tracker.example"}))

    def test_strict_parser_supports_trackerless_and_deduplicates_trackers(self):
        raw = torrent_bytes(
            announce_list=[
                ["udp://tracker.example:80/announce"],
                ["https://tracker.example/announce"],
            ]
        )
        parsed = parse_torrent(raw)
        self.assertEqual(
            parsed.trackers,
            (
                "https://tracker.example/announce",
                "udp://tracker.example:80/announce",
            ),
        )
        self.assertIn("&tr=", parsed.magnet_uri)

        trackerless = parse_torrent(torrent_bytes(announce=None))
        self.assertEqual(trackerless.trackers, ())
        self.assertNotIn("&tr=", trackerless.magnet_uri)

    def test_strict_parser_rejects_invalid_root_info_and_bencode(self):
        oversized_length = str(torrent_module.MAX_BENCODE_STRING + 1).encode() + b":"
        too_deep = b"l" * (torrent_module.MAX_BENCODE_DEPTH + 2) + b"e" * (
            torrent_module.MAX_BENCODE_DEPTH + 2
        )
        too_many_list_values = (
            b"l" + b"0:" * (torrent_module.MAX_BENCODE_ITEMS + 1) + b"e"
        )
        dictionary_entries = b"".join(
            bencode(f"{index:05d}".encode()) + b"0:"
            for index in range(torrent_module.MAX_BENCODE_ITEMS + 1)
        )
        cases = [
            b"",
            b"0:",
            b"degarbage",
            b"de",
            b"d4:info0:e",
            bencode({b"info": {b"files": []}}),
            b"x",
            b"i1",
            b"ie",
            b"i-0e",
            b"i+1e",
            b"i01e",
            b"i-e",
            b"i9223372036854775808e",
            b"1",
            b"01:a",
            b"a:a",
            oversized_length,
            b"2:a",
            b"l",
            too_deep,
            too_many_list_values,
            b"d",
            b"d1:a",
            b"di1e0:e",
            b"d1:b0:1:a0:e",
            b"d1:a0:1:a0:e",
            b"d" + dictionary_entries + b"e",
        ]
        for raw in cases:
            with self.subTest(raw=raw[:30]):
                self.assert_invalid(raw)

    def test_strict_parser_rejects_invalid_piece_geometry(self):
        valid = {
            b"length": 100,
            b"name": b"payload.zip",
            b"piece length": 16 * 1024,
            b"pieces": b"p" * 20,
        }
        cases = []
        for key, value in (
            (b"length", 0),
            (b"piece length", 0),
            (b"piece length", 1000),
            (b"piece length", torrent_module.MAX_PIECE_LENGTH * 2),
            (b"pieces", b""),
            (b"pieces", 1),
            (b"pieces", b"bad"),
            (b"pieces", b"p" * 40),
        ):
            info = dict(valid)
            info[key] = value
            cases.append(bencode({b"info": info}))
        for raw in cases:
            with self.subTest(raw=raw[-50:]):
                self.assert_invalid(raw)

    def test_strict_parser_rejects_unsafe_file_names(self):
        for name in (
            b"",
            b"a" * 256,
            b"\xff",
            b".",
            b"..",
            b"folder/file.zip",
            b"folder\\file.zip",
            b"nul\x00.zip",
            b"line\n.zip",
            b"delete\x7f.zip",
        ):
            with self.subTest(name=name):
                self.assert_invalid(
                    bencode(
                        {
                            b"info": {
                                b"length": 1,
                                b"name": name,
                                b"piece length": 16 * 1024,
                                b"pieces": b"p" * 20,
                            }
                        }
                    )
                )

    def test_strict_parser_rejects_malformed_trackers(self):
        valid_info = {
            b"length": 1,
            b"name": b"payload.zip",
            b"piece length": 16 * 1024,
            b"pieces": b"p" * 20,
        }
        documents: list[dict[bytes, object]] = [
            {b"announce-list": b"nope", b"info": valid_info},
            {b"announce-list": [b"no-tier"], b"info": valid_info},
            {b"announce": 4, b"info": valid_info},
            {b"announce": b"\xff", b"info": valid_info},
        ]
        documents.extend(
            {b"announce": tracker.encode(), b"info": valid_info}
            for tracker in (
                "x" * 2049,
                "ftp://tracker.example/announce",
                "https:///missing-host",
                "https://user@tracker.example/announce",
                "https://user:pass@tracker.example/announce",
                "https://tracker.example/line\nfeed",
            )
        )
        documents.append(
            {
                b"announce-list": [
                    [f"https://tracker-{index}.example/announce"] for index in range(65)
                ],
                b"info": valid_info,
            }
        )
        for document in documents:
            with self.subTest(keys=document.keys()):
                self.assert_invalid(bencode(document))

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

    def test_piece_hashing_is_cancellable_without_a_partial_torrent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload.bin"
            payload.write_bytes(b"x" * 32768)
            destination = root / "payload.torrent"
            stop = Mock()
            stop.is_set.side_effect = [False, True]

            with self.assertRaises(AcquisitionCancelled):
                create_torrent(
                    payload,
                    destination,
                    piece_length=16384,
                    stop=stop,
                )

            self.assertFalse(destination.exists())
