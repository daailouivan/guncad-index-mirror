from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event
from typing import TypeAlias
from urllib.parse import quote, urlsplit

from .cancellation import check_cancelled
from .models import TorrentArtifact

Bencodable: TypeAlias = (
    bytes | str | int | list["Bencodable"] | dict[bytes | str, "Bencodable"]
)
CREATED_BY = "GunCAD Mirror"
MAX_BENCODE_DEPTH = 16
MAX_BENCODE_ITEMS = 4096
MAX_BENCODE_STRING = 4 * 1024 * 1024
MAX_PIECE_LENGTH = 16 * 1024 * 1024


class TorrentError(ValueError):
    """Torrent metainfo cannot be generated safely."""


@dataclass(frozen=True, slots=True)
class ParsedTorrent:
    info_hash: str
    torrent_sha256: str
    file_name: str
    file_length: int
    piece_length: int
    piece_count: int
    trackers: tuple[str, ...]
    magnet_uri: str


class _BencodeDecoder:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.offset = 0
        self.info_span: tuple[int, int] | None = None

    def decode(self) -> object:
        value = self._value(0)
        if self.offset != len(self.raw):
            raise TorrentError("metainfo contains trailing data")
        return value

    def _value(self, depth: int) -> object:
        if depth > MAX_BENCODE_DEPTH:
            raise TorrentError("metainfo nesting is too deep")
        if self.offset >= len(self.raw):
            raise TorrentError("metainfo ended unexpectedly")
        marker = self.raw[self.offset]
        if marker == ord("i"):
            return self._integer()
        if marker == ord("l"):
            return self._list(depth)
        if marker == ord("d"):
            return self._dictionary(depth)
        if ord("0") <= marker <= ord("9"):
            return self._bytes()
        raise TorrentError("metainfo contains an invalid bencode marker")

    def _integer(self) -> int:
        self.offset += 1
        end = self.raw.find(b"e", self.offset)
        if end < 0:
            raise TorrentError("unterminated bencode integer")
        encoded = self.raw[self.offset : end]
        if not encoded:
            raise TorrentError("empty bencode integer")
        if encoded == b"-0" or encoded.startswith(b"+"):
            raise TorrentError("non-canonical bencode integer")
        digits = encoded[1:] if encoded.startswith(b"-") else encoded
        if not digits.isdigit() or (len(digits) > 1 and digits.startswith(b"0")):
            raise TorrentError("non-canonical bencode integer")
        value = int(encoded)
        if not -(2**63) <= value < 2**63:
            raise TorrentError("bencode integer is out of range")
        self.offset = end + 1
        return value

    def _bytes(self) -> bytes:
        colon = self.raw.find(b":", self.offset)
        if colon < 0:
            raise TorrentError("invalid bencode string length")
        encoded_length = self.raw[self.offset : colon]
        if not encoded_length.isdigit() or (
            len(encoded_length) > 1 and encoded_length.startswith(b"0")
        ):
            raise TorrentError("non-canonical bencode string length")
        length = int(encoded_length)
        if length > MAX_BENCODE_STRING:
            raise TorrentError("bencode string is too large")
        start = colon + 1
        end = start + length
        if end > len(self.raw):
            raise TorrentError("bencode string exceeds metainfo length")
        self.offset = end
        return self.raw[start:end]

    def _list(self, depth: int) -> list[object]:
        self.offset += 1
        values: list[object] = []
        while True:
            if self.offset >= len(self.raw):
                raise TorrentError("unterminated bencode list")
            if self.raw[self.offset] == ord("e"):
                self.offset += 1
                return values
            if len(values) >= MAX_BENCODE_ITEMS:
                raise TorrentError("bencode list contains too many values")
            values.append(self._value(depth + 1))

    def _dictionary(self, depth: int) -> dict[bytes, object]:
        self.offset += 1
        values: dict[bytes, object] = {}
        previous_key: bytes | None = None
        while True:
            if self.offset >= len(self.raw):
                raise TorrentError("unterminated bencode dictionary")
            if self.raw[self.offset] == ord("e"):
                self.offset += 1
                return values
            if len(values) >= MAX_BENCODE_ITEMS:
                raise TorrentError("bencode dictionary contains too many values")
            if not ord("0") <= self.raw[self.offset] <= ord("9"):
                raise TorrentError("bencode dictionary key is not a string")
            key = self._bytes()
            if previous_key is not None and key <= previous_key:
                raise TorrentError("bencode dictionary keys are duplicated or unsorted")
            previous_key = key
            value_start = self.offset
            value = self._value(depth + 1)
            if depth == 0 and key == b"info":
                self.info_span = (value_start, self.offset)
            values[key] = value


def parse_torrent(raw: bytes) -> ParsedTorrent:
    """Parse the strict single-file v1 subset accepted by GunCAD Index."""

    if not raw:
        raise TorrentError("torrent is empty")
    decoder = _BencodeDecoder(raw)
    metainfo = decoder.decode()
    if not isinstance(metainfo, dict):
        raise TorrentError("metainfo must be a dictionary")
    info = metainfo.get(b"info")
    if not isinstance(info, dict) or decoder.info_span is None:
        raise TorrentError("metainfo has no info dictionary")
    if b"files" in info:
        raise TorrentError("multi-file torrents are not supported")

    file_length = _parsed_positive_integer(info, b"length")
    piece_length = _parsed_positive_integer(info, b"piece length")
    if (
        piece_length < 16 * 1024
        or piece_length > MAX_PIECE_LENGTH
        or piece_length & (piece_length - 1)
    ):
        raise TorrentError("piece length must be a supported power of two")

    pieces = info.get(b"pieces")
    if not isinstance(pieces, bytes) or not pieces or len(pieces) % 20:
        raise TorrentError("pieces must contain complete SHA-1 digests")
    piece_count = len(pieces) // 20
    if piece_count != math.ceil(file_length / piece_length):
        raise TorrentError("piece geometry does not match the file length")

    file_name = _parsed_file_name(info.get(b"name"))
    trackers = _parsed_trackers(metainfo)
    info_start, info_end = decoder.info_span
    info_hash = hashlib.sha1(
        raw[info_start:info_end], usedforsecurity=False
    ).hexdigest()
    magnet_parts = [
        f"xt=urn:btih:{info_hash}",
        f"dn={quote(file_name, safe='')}",
        *(f"tr={quote(tracker, safe='')}" for tracker in trackers),
    ]
    return ParsedTorrent(
        info_hash=info_hash,
        torrent_sha256=hashlib.sha256(raw).hexdigest(),
        file_name=file_name,
        file_length=file_length,
        piece_length=piece_length,
        piece_count=piece_count,
        trackers=trackers,
        magnet_uri="magnet:?" + "&".join(magnet_parts),
    )


def strip_torrent_trackers(raw: bytes) -> bytes:
    """Remove tracker hints without changing the exact encoded info dictionary."""

    decoder = _BencodeDecoder(raw)
    metainfo = decoder.decode()
    if (
        not isinstance(metainfo, dict)
        or not isinstance(metainfo.get(b"info"), dict)
        or decoder.info_span is None
    ):
        raise TorrentError("metainfo has no info dictionary")
    if b"announce" not in metainfo and b"announce-list" not in metainfo:
        return raw

    info_start, info_end = decoder.info_span
    sanitized = {
        key: value
        for key, value in metainfo.items()
        if key not in {b"announce", b"announce-list"}
    }
    encoded: list[bytes] = [b"d"]
    for key in sorted(sanitized):
        encoded.append(bencode(key))
        encoded.append(
            raw[info_start:info_end] if key == b"info" else bencode(sanitized[key])  # type: ignore[arg-type]
        )
    encoded.append(b"e")
    return b"".join(encoded)


def _parsed_positive_integer(mapping: dict[bytes, object], key: bytes) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TorrentError(f"{key.decode()} must be a positive integer")
    return value


def _parsed_file_name(value: object) -> str:
    if not isinstance(value, bytes) or not value or len(value) > 255:
        raise TorrentError("torrent file name is invalid")
    try:
        result = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TorrentError("torrent file name is not UTF-8") from error
    if result in {".", ".."} or any(
        character in result for character in ("/", "\\", "\x00")
    ):
        raise TorrentError("torrent file name contains a path")
    if any(ord(character) < 32 or ord(character) == 127 for character in result):
        raise TorrentError("torrent file name contains control characters")
    return result


def _parsed_trackers(metainfo: dict[bytes, object]) -> tuple[str, ...]:
    raw_trackers: list[object] = []
    announce = metainfo.get(b"announce")
    if announce is not None:
        raw_trackers.append(announce)
    announce_list = metainfo.get(b"announce-list", [])
    if not isinstance(announce_list, list):
        raise TorrentError("announce-list must be a list")
    for tier in announce_list:
        if not isinstance(tier, list):
            raise TorrentError("announce-list tiers must be lists")
        raw_trackers.extend(tier)

    trackers: list[str] = []
    for raw_tracker in raw_trackers:
        if not isinstance(raw_tracker, bytes):
            raise TorrentError("tracker URLs must be strings")
        try:
            tracker = raw_tracker.decode("utf-8")
        except UnicodeDecodeError as error:
            raise TorrentError("tracker URL is not UTF-8") from error
        parsed = urlsplit(tracker)
        if (
            len(tracker) > 2048
            or any(
                ord(character) < 32 or ord(character) == 127 for character in tracker
            )
            or parsed.scheme not in {"http", "https", "udp"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise TorrentError("tracker URL is invalid")
        if tracker not in trackers:
            trackers.append(tracker)
        if len(trackers) > 64:
            raise TorrentError("torrent contains too many trackers")
    return tuple(trackers)


def bencode(value: Bencodable) -> bytes:
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        return bencode(value.encode("utf-8"))
    if type(value) is int:
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        encoded_items: list[bytes] = []
        keys: list[tuple[bytes, bytes | str]] = []
        seen_keys: set[bytes] = set()
        for original_key in value:
            if not isinstance(original_key, (bytes, str)):
                raise TorrentError("bencoded dictionary keys must be bytes or strings")
            encoded_key = (
                original_key.encode("utf-8")
                if isinstance(original_key, str)
                else original_key
            )
            if encoded_key in seen_keys:
                raise TorrentError("bencoded dictionary contains duplicate byte keys")
            seen_keys.add(encoded_key)
            keys.append((encoded_key, original_key))
        for encoded_key, original_key in sorted(keys, key=lambda item: item[0]):
            encoded_items.append(bencode(encoded_key))
            encoded_items.append(bencode(value[original_key]))
        return b"d" + b"".join(encoded_items) + b"e"
    raise TorrentError(f"cannot bencode {type(value).__name__}")


def create_torrent(
    file_path: Path,
    output_path: Path,
    *,
    piece_length: int = 1024**2,
    stop: Event | None = None,
    progress: Callable[[int], None] | None = None,
) -> TorrentArtifact:
    check_cancelled(stop)
    if not file_path.is_file():
        raise TorrentError(f"payload does not exist: {file_path}")
    if piece_length < 16 * 1024 or piece_length & (piece_length - 1):
        raise TorrentError("piece length must be a power of two of at least 16 KiB")

    pieces, piece_count = _hash_pieces(
        file_path,
        piece_length,
        stop=stop,
        progress=progress,
    )
    info: dict[bytes, Bencodable] = {
        b"length": file_path.stat().st_size,
        b"name": file_path.name,
        b"piece length": piece_length,
        b"pieces": pieces,
    }
    metainfo: dict[bytes, Bencodable] = {b"created by": CREATED_BY, b"info": info}

    info_hash = hashlib.sha1(bencode(info), usedforsecurity=False).hexdigest()
    torrent_bytes = bencode(metainfo)
    torrent_sha256 = hashlib.sha256(torrent_bytes).hexdigest()
    _atomic_write(output_path, torrent_bytes)

    query = [f"xt=urn:btih:{info_hash}", f"dn={quote(file_path.name, safe='')}"]
    return TorrentArtifact(
        file_path=file_path,
        torrent_path=output_path,
        piece_length=piece_length,
        piece_count=piece_count,
        info_hash=info_hash,
        torrent_sha256=torrent_sha256,
        magnet_uri="magnet:?" + "&".join(query),
        trackers=(),
    )


def _hash_pieces(
    file_path: Path,
    piece_length: int,
    *,
    stop: Event | None = None,
    progress: Callable[[int], None] | None = None,
) -> tuple[bytes, int]:
    hashes = bytearray()
    piece_count = 0
    completed = 0
    if progress is not None:
        progress(completed)
    with file_path.open("rb") as payload:
        while piece := payload.read(piece_length):
            check_cancelled(stop)
            hashes.extend(hashlib.sha1(piece, usedforsecurity=False).digest())
            piece_count += 1
            completed += len(piece)
            if progress is not None:
                progress(completed)
    if piece_count == 0:
        raise TorrentError("cannot create a torrent for an empty file")
    return bytes(hashes), piece_count


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temp:
        temporary_path = Path(temp.name)
        temp.write(content)
        temp.flush()
        os.fsync(temp.fileno())
    temporary_path.replace(path)
