from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from guncadmirror.archive_inspector import (
    ArchiveFileEntry,
    ArchiveNotFoundError,
    InvalidEntryPathError,
    NotAZipArchiveError,
    ZipEntryStream,
    extract_archive_file,
    inspect_archive,
    sanitize_zip_path,
)


class TestArchiveInspector(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sanitize_zip_path(self) -> None:
        self.assertEqual(sanitize_zip_path("file.txt"), "file.txt")
        self.assertEqual(sanitize_zip_path("/models/part.stl"), "models/part.stl")
        self.assertEqual(
            sanitize_zip_path("sub\\folder\\doc.pdf"), "sub/folder/doc.pdf"
        )
        self.assertEqual(sanitize_zip_path("./cad/model.step"), "cad/model.step")
        self.assertEqual(sanitize_zip_path("folder/"), "folder/")

        with self.assertRaises(InvalidEntryPathError):
            sanitize_zip_path("")
        with self.assertRaises(InvalidEntryPathError):
            sanitize_zip_path("   ")
        with self.assertRaises(InvalidEntryPathError):
            sanitize_zip_path("///")
        with self.assertRaises(InvalidEntryPathError):
            sanitize_zip_path("../secret.txt")
        with self.assertRaises(InvalidEntryPathError):
            sanitize_zip_path("sub/../../secret.txt")

    def test_archive_file_entry_to_dict(self) -> None:
        entry = ArchiveFileEntry(
            path="models/part.stl",
            name="part.stl",
            size=1024,
            compressed_size=512,
            is_dir=False,
            extension=".stl",
            modified="2026-10-17 12:00",
        )
        d = entry.to_dict()
        self.assertEqual(d["path"], "models/part.stl")
        self.assertEqual(d["name"], "part.stl")
        self.assertEqual(d["size"], 1024)
        self.assertEqual(d["compressed_size"], 512)
        self.assertFalse(d["is_dir"])
        self.assertEqual(d["extension"], ".stl")
        self.assertEqual(d["modified"], "2026-10-17 12:00")

    def test_inspect_archive_not_found(self) -> None:
        with self.assertRaises(ArchiveNotFoundError):
            inspect_archive(self.dir_path / "nonexistent.zip")

    def test_inspect_archive_not_a_zip(self) -> None:
        not_zip = self.dir_path / "plain.txt"
        not_zip.write_text("Hello world", encoding="utf-8")
        entries, total_size, is_zip = inspect_archive(not_zip)
        self.assertFalse(is_zip)
        self.assertEqual(entries, [])
        self.assertEqual(total_size, 0)

    def test_inspect_archive_success(self) -> None:
        zip_path = self.dir_path / "release.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("readme.txt", b"Instructions for assembling.")
            zf.writestr("models/frame.stl", b"solid frame STL data")
            zf.writestr("docs/", b"")

        entries, total_size, is_zip = inspect_archive(zip_path)
        self.assertTrue(is_zip)
        self.assertGreater(total_size, 0)
        self.assertEqual(len(entries), 3)

        filenames = [e.path for e in entries]
        self.assertIn("readme.txt", filenames)
        self.assertIn("models/frame.stl", filenames)
        self.assertIn("docs/", filenames)

        stl_entry = next(e for e in entries if e.path == "models/frame.stl")
        self.assertEqual(stl_entry.name, "frame.stl")
        self.assertEqual(stl_entry.extension, ".stl")
        self.assertFalse(stl_entry.is_dir)
        self.assertIsNotNone(stl_entry.modified)

        dir_entry = next(e for e in entries if e.path == "docs/")
        self.assertTrue(dir_entry.is_dir)
        self.assertEqual(dir_entry.extension, "")

    def test_inspect_archive_skips_invalid_paths_and_limits(self) -> None:
        zip_path = self.dir_path / "test_limit.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for i in range(10):
                zf.writestr(f"file_{i}.bin", b"data")

        with patch("guncadmirror.archive_inspector.MAX_INSPECT_ENTRIES", 3):
            entries, _, is_zip = inspect_archive(zip_path)
            self.assertTrue(is_zip)
            self.assertEqual(len(entries), 3)

    def test_extract_archive_file_not_found(self) -> None:
        with self.assertRaises(ArchiveNotFoundError):
            extract_archive_file(self.dir_path / "missing.zip", "test.stl")

    def test_extract_archive_file_not_a_zip(self) -> None:
        not_zip = self.dir_path / "data.bin"
        not_zip.write_bytes(b"\x00\x01\x02")
        with self.assertRaises(NotAZipArchiveError):
            extract_archive_file(not_zip, "test.stl")

    def test_extract_archive_file_traversal_rejection(self) -> None:
        zip_path = self.dir_path / "payload.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("normal.stl", b"stl content")

        with self.assertRaises(InvalidEntryPathError):
            extract_archive_file(zip_path, "../secret.txt")

    def test_extract_archive_file_key_error(self) -> None:
        zip_path = self.dir_path / "payload.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("normal.stl", b"stl content")

        with self.assertRaises(KeyError):
            extract_archive_file(zip_path, "missing_file.stl")

    def test_extract_archive_file_directory_rejection(self) -> None:
        zip_path = self.dir_path / "payload.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("cad/", b"")

        with self.assertRaises(InvalidEntryPathError):
            extract_archive_file(zip_path, "cad/")

    def test_extract_archive_file_oversized(self) -> None:
        zip_path = self.dir_path / "oversized.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("big.bin", b"fake big data")

        with patch("guncadmirror.archive_inspector.MAX_EXTRACT_BYTES", 5):
            with self.assertRaises(ValueError):
                extract_archive_file(zip_path, "big.bin")

    def test_extract_archive_file_success(self) -> None:
        zip_path = self.dir_path / "cad.zip"
        content = b"3D MODEL BINARY STL CONTENT"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("models/frame.stl", content)
            zf.writestr("notes.txt", b"plain text")

        fp, size, content_type = extract_archive_file(zip_path, "models/frame.stl")
        try:
            self.assertEqual(size, len(content))
            self.assertEqual(fp.read(), content)
            self.assertEqual(content_type, "model/stl")
        finally:
            fp.close()

        # Test case-insensitive or stripped slash match fallback
        fp2, size2, ct2 = extract_archive_file(zip_path, "notes.txt")
        try:
            self.assertEqual(fp2.read(), b"plain text")
            self.assertEqual(ct2, "text/plain")
        finally:
            fp2.close()

    def test_extract_archive_file_unknown_mimetype_and_fallback(self) -> None:
        zip_path = self.dir_path / "custom.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("./docs/manual.unknownext", b"custom data")
            zf.writestr("../../evil.txt", b"evil data")

        # Fallback candidate match for clean_path
        fp, size, ct = extract_archive_file(zip_path, "docs/manual.unknownext")
        try:
            self.assertEqual(fp.read(), b"custom data")
            self.assertEqual(ct, "application/octet-stream")
        finally:
            fp.close()

        # Searching for missing file when archive contains malformed path
        with self.assertRaises(KeyError):
            extract_archive_file(zip_path, "does_not_exist.stl")

    def test_inspect_archive_skips_malicious_path_entries(self) -> None:
        zip_path = self.dir_path / "unsafe.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../../traversal.txt", b"bad")
            zf.writestr("good.txt", b"good")

        entries, _, is_zip = inspect_archive(zip_path)
        self.assertTrue(is_zip)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, "good.txt")

    def test_sanitize_zip_path_dots_only(self) -> None:
        with self.assertRaises(InvalidEntryPathError):
            sanitize_zip_path("/././")

    def test_zip_entry_stream_wrapper(self) -> None:
        mock_zf = MagicMock()
        mock_ext = MagicMock()
        mock_ext.read.return_value = b"test"
        mock_ext.readinto.return_value = 4
        mock_ext.seek.return_value = 0
        mock_ext.tell.return_value = 4
        mock_ext.seekable.return_value = True
        mock_ext.readable.return_value = True

        stream = ZipEntryStream(mock_zf, mock_ext)
        self.assertEqual(stream.read(10), b"test")
        buf = bytearray(4)
        self.assertEqual(stream.readinto(buf), 4)
        self.assertEqual(stream.seek(0, io.SEEK_SET), 0)
        self.assertEqual(stream.tell(), 4)
        self.assertTrue(stream.seekable())
        self.assertTrue(stream.readable())

        stream.close()
        mock_ext.close.assert_called_once()
        mock_zf.close.assert_called_once()
