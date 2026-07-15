from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

SHA384_RE = re.compile(r"^[0-9a-f]{96}$")
CLAIM_ID_RE = re.compile(r"^[0-9a-f]{40}$")


class ReleaseValidationError(ValueError):
    """The Index returned a release that cannot be mirrored safely."""


class JobState(StrEnum):
    PENDING = "pending"
    ACQUIRING = "acquiring"
    VERIFIED = "verified"
    AWAITING_INDEX = "awaiting_index"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Release:
    id: str
    name: str
    url: str
    url_lbry: str
    channel_handle: str
    sd_hash: str
    sha384: str | None
    size: int | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_api(cls, value: Mapping[str, Any]) -> Release:
        if not isinstance(value, Mapping):
            raise ReleaseValidationError("release must be a JSON object")

        release_id = _required_string(value, "id")
        if not CLAIM_ID_RE.fullmatch(release_id):
            raise ReleaseValidationError("release id must be a 40-character claim id")

        channel = value.get("channel")
        if not isinstance(channel, Mapping):
            raise ReleaseValidationError("release channel must be a JSON object")

        sd_hash = _required_string(value, "sd_hash")
        if not SHA384_RE.fullmatch(sd_hash):
            raise ReleaseValidationError("sd_hash must be a lowercase SHA-384 digest")

        sha384 = value.get("sha384sum")
        if sha384 in (None, ""):
            sha384 = None
        elif not isinstance(sha384, str) or not SHA384_RE.fullmatch(sha384):
            raise ReleaseValidationError("sha384sum must be a lowercase SHA-384 digest")

        size = value.get("size")
        if size is not None:
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise ReleaseValidationError("release size must be a positive integer")

        return cls(
            id=release_id,
            name=_required_string(value, "name"),
            url=_required_string(value, "url"),
            url_lbry=_required_string(value, "url_lbry"),
            channel_handle=_required_string(channel, "handle"),
            sd_hash=sd_hash,
            sha384=sha384,
            size=size,
            raw=dict(value),
        )

    def to_json(self) -> str:
        return json.dumps(self.raw, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class FileHashes:
    size: int
    sha384: str
    sha256: str


@dataclass(frozen=True, slots=True)
class TorrentArtifact:
    file_path: Path
    torrent_path: Path
    piece_length: int
    piece_count: int
    info_hash: str
    torrent_sha256: str
    magnet_uri: str
    trackers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublicationBundle:
    release: Release
    hashes: FileHashes
    torrent: TorrentArtifact
    manifest_path: Path


def _required_string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ReleaseValidationError(f"{key} must be a non-empty string")
    return result
