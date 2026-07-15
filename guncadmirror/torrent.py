from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypeAlias
from urllib.parse import quote

from .models import TorrentArtifact

Bencodable: TypeAlias = (
    bytes | str | int | list["Bencodable"] | dict[bytes | str, "Bencodable"]
)
CREATED_BY = "GunCAD Mirror"


class TorrentError(ValueError):
    """Torrent metainfo cannot be generated safely."""


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
    trackers: tuple[str, ...] = (),
) -> TorrentArtifact:
    if not file_path.is_file():
        raise TorrentError(f"payload does not exist: {file_path}")
    if piece_length < 16 * 1024 or piece_length & (piece_length - 1):
        raise TorrentError("piece length must be a power of two of at least 16 KiB")

    pieces, piece_count = _hash_pieces(file_path, piece_length)
    info: dict[bytes, Bencodable] = {
        b"length": file_path.stat().st_size,
        b"name": file_path.name,
        b"piece length": piece_length,
        b"pieces": pieces,
    }
    metainfo: dict[bytes, Bencodable] = {b"created by": CREATED_BY, b"info": info}
    if trackers:
        metainfo[b"announce"] = trackers[0]
        metainfo[b"announce-list"] = [[tracker] for tracker in trackers]

    info_hash = hashlib.sha1(bencode(info), usedforsecurity=False).hexdigest()
    torrent_bytes = bencode(metainfo)
    torrent_sha256 = hashlib.sha256(torrent_bytes).hexdigest()
    _atomic_write(output_path, torrent_bytes)

    query = [f"xt=urn:btih:{info_hash}", f"dn={quote(file_path.name, safe='')}"]
    query.extend(f"tr={quote(tracker, safe='')}" for tracker in trackers)
    return TorrentArtifact(
        file_path=file_path,
        torrent_path=output_path,
        piece_length=piece_length,
        piece_count=piece_count,
        info_hash=info_hash,
        torrent_sha256=torrent_sha256,
        magnet_uri="magnet:?" + "&".join(query),
        trackers=trackers,
    )


def _hash_pieces(file_path: Path, piece_length: int) -> tuple[bytes, int]:
    hashes = bytearray()
    piece_count = 0
    with file_path.open("rb") as payload:
        while piece := payload.read(piece_length):
            hashes.extend(hashlib.sha1(piece, usedforsecurity=False).digest())
            piece_count += 1
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
