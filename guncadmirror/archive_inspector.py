from __future__ import annotations

import io
import mimetypes
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

# Register common GunCAD mime types
mimetypes.add_type("model/stl", ".stl")
mimetypes.add_type("model/step", ".step")
mimetypes.add_type("model/step", ".stp")
mimetypes.add_type("model/3mf", ".3mf")

MAX_INSPECT_ENTRIES = 2000
MAX_EXTRACT_BYTES = 512 * 1024 * 1024  # 512 MB


class ArchiveInspectorError(Exception):
    """Base error for archive inspection."""


class ArchiveNotFoundError(ArchiveInspectorError):
    """Payload or archive not found on disk."""


class NotAZipArchiveError(ArchiveInspectorError):
    """The target payload file is not a valid zip archive."""


class InvalidEntryPathError(ArchiveInspectorError):
    """The requested file path inside the archive is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class ArchiveFileEntry:
    path: str
    name: str
    size: int
    compressed_size: int
    is_dir: bool
    extension: str
    modified: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "compressed_size": self.compressed_size,
            "is_dir": self.is_dir,
            "extension": self.extension,
            "modified": self.modified,
        }


class ZipEntryStream(io.RawIOBase):
    """Wraps a ZipExtFile to ensure both the member stream and parent ZipFile are closed."""

    def __init__(self, zf: zipfile.ZipFile, ext_file: BinaryIO) -> None:
        self._zf = zf
        self._ext_file = ext_file

    def read(self, size: int = -1) -> bytes:
        return self._ext_file.read(size)

    def readinto(self, b: Any) -> int:
        return self._ext_file.readinto(b)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._ext_file.seek(offset, whence)

    def tell(self) -> int:
        return self._ext_file.tell()

    def seekable(self) -> bool:
        return self._ext_file.seekable()

    def readable(self) -> bool:
        return self._ext_file.readable()

    def close(self) -> None:
        try:
            self._ext_file.close()
        finally:
            self._zf.close()
            super().close()


def sanitize_zip_path(raw_path: str) -> str:
    """Normalize and validate a relative path inside a zip archive."""
    normalized = raw_path.strip().replace("\\", "/").lstrip("/")
    if not normalized:
        raise InvalidEntryPathError("Empty entry path")

    parts = normalized.split("/")
    if any(p == ".." for p in parts):
        raise InvalidEntryPathError(f"Unsafe path traversal in archive: {raw_path}")

    clean_parts = [p for p in parts if p and p != "."]
    if not clean_parts:
        raise InvalidEntryPathError(f"Invalid entry path: {raw_path}")

    clean_path = "/".join(clean_parts)
    if raw_path.endswith("/"):
        clean_path += "/"
    return clean_path


def inspect_archive(archive_path: Path) -> tuple[list[ArchiveFileEntry], int, bool]:
    """Inspects a zip archive safely and returns entries, total uncompressed size, and whether it's a zip."""
    if not archive_path.is_file():
        raise ArchiveNotFoundError(f"Archive file not found: {archive_path}")

    if not zipfile.is_zipfile(archive_path):
        return [], 0, False

    entries: list[ArchiveFileEntry] = []
    total_uncompressed = 0

    with zipfile.ZipFile(archive_path, "r") as zf:
        infolist = zf.infolist()
        for i, info in enumerate(infolist):
            if i >= MAX_INSPECT_ENTRIES:
                break
            try:
                clean_path = sanitize_zip_path(info.filename)
            except InvalidEntryPathError:
                continue

            name = Path(clean_path).name or clean_path
            is_dir = info.is_dir()
            ext = Path(clean_path).suffix.lower() if not is_dir else ""
            total_uncompressed += info.file_size
            mod_str = None
            if info.date_time:
                mod_str = (
                    f"{info.date_time[0]:04d}-{info.date_time[1]:02d}-{info.date_time[2]:02d} "
                    f"{info.date_time[3]:02d}:{info.date_time[4]:02d}"
                )

            entries.append(
                ArchiveFileEntry(
                    path=clean_path,
                    name=name,
                    size=info.file_size,
                    compressed_size=info.compress_size,
                    is_dir=is_dir,
                    extension=ext,
                    modified=mod_str,
                )
            )

    return entries, total_uncompressed, True


def extract_archive_file(
    archive_path: Path, entry_path: str
) -> tuple[BinaryIO, int, str]:
    """Extracts a file from a zip archive safely and returns a binary stream, size, and content type."""
    if not archive_path.is_file():
        raise ArchiveNotFoundError(f"Archive file not found: {archive_path}")

    if not zipfile.is_zipfile(archive_path):
        raise NotAZipArchiveError(f"Target is not a zip archive: {archive_path}")

    clean_path = sanitize_zip_path(entry_path)

    zf = zipfile.ZipFile(archive_path, "r")
    try:
        try:
            info = zf.getinfo(clean_path)
        except KeyError:
            for candidate in zf.namelist():
                try:
                    if sanitize_zip_path(candidate) == clean_path:
                        info = zf.getinfo(candidate)
                        break
                except InvalidEntryPathError:
                    continue
            else:
                zf.close()
                raise KeyError(f"File not found in archive: {clean_path}")

        if info.is_dir():
            zf.close()
            raise InvalidEntryPathError(f"Cannot extract directory: {clean_path}")

        if info.file_size > MAX_EXTRACT_BYTES:
            zf.close()
            raise ValueError(
                f"File size {info.file_size} exceeds maximum single extraction limit ({MAX_EXTRACT_BYTES})"
            )

        fp = zf.open(info, "r")
        content_type, _ = mimetypes.guess_type(clean_path)
        if not content_type:
            content_type = "application/octet-stream"

        wrapped = ZipEntryStream(zf, fp)
        return wrapped, info.file_size, content_type
    except Exception:
        zf.close()
        raise
