from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

SHA384_RE = re.compile(r"^[0-9a-f]{96}$")
CLAIM_ID_RE = re.compile(r"^[0-9a-f]{40}$")


class ReleaseValidationError(ValueError):
    """The Index returned a release that cannot be mirrored safely."""


class UnsupportedOriginError(ReleaseValidationError):
    """The Index release uses a transport that Mirror does not support."""


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
    url: str | None
    url_lbry: str
    channel_handle: str
    sd_hash: str
    sha384: str
    size: int
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_api(cls, value: Mapping[str, Any]) -> Release:
        if not isinstance(value, Mapping):
            raise ReleaseValidationError("release must be a JSON object")

        origin = value.get("origin")
        if not isinstance(origin, Mapping):
            raise ReleaseValidationError("release origin must be a JSON object")
        platform = _required_string(origin, "platform")
        if platform != "lbry":
            raise UnsupportedOriginError(f"unsupported release origin: {platform}")

        release_id = _required_string(value, "id")
        if not CLAIM_ID_RE.fullmatch(release_id):
            raise ReleaseValidationError("release id must be a 40-character claim id")

        channel = value.get("channel")
        if not isinstance(channel, Mapping):
            raise ReleaseValidationError("release channel must be a JSON object")

        if _required_string(origin, "external_id") != release_id:
            raise ReleaseValidationError("origin external_id must match release id")

        extra = origin.get("extra")
        if not isinstance(extra, Mapping):
            raise ReleaseValidationError("release origin extra must be a JSON object")

        sd_hash = _required_string(extra, "sd_hash")
        if not SHA384_RE.fullmatch(sd_hash):
            raise ReleaseValidationError("sd_hash must be a lowercase SHA-384 digest")

        sha384 = _required_string(origin, "checksum")
        if not SHA384_RE.fullmatch(sha384):
            raise ReleaseValidationError(
                "origin checksum must be a lowercase SHA-384 digest"
            )

        size = origin.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ReleaseValidationError("origin size must be a positive integer")

        links = origin.get("links")
        if not isinstance(links, list):
            raise ReleaseValidationError("release origin links must be a list")
        url = _optional_link_for_schemes(links, ("https", "http"))
        url_lbry = unquote(_link_for_schemes(links, ("lbry",), "LBRY"))

        return cls(
            id=release_id,
            name=_required_string(value, "name"),
            url=url,
            url_lbry=url_lbry,
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


def _link_for_schemes(links: list[Any], schemes: tuple[str, ...], label: str) -> str:
    result = _optional_link_for_schemes(links, schemes)
    if result is not None:
        return result
    raise ReleaseValidationError(f"release origin has no valid {label} link")


def _optional_link_for_schemes(
    links: list[Any], schemes: tuple[str, ...]
) -> str | None:
    for scheme in schemes:
        for link in links:
            if not isinstance(link, Mapping):
                continue
            url = link.get("url")
            if not isinstance(url, str) or not url:
                continue
            parsed = urlsplit(url)
            if parsed.scheme.lower() == scheme and parsed.netloc:
                if parsed.username or parsed.password:
                    continue
                return url
    return None
