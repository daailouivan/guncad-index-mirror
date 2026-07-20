from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from .models import (
    AcquisitionEvidence,
    FileHashes,
    PublicationBundle,
    Release,
    TorrentArtifact,
)


class Publisher(Protocol):
    def publish(
        self,
        release: Release,
        hashes: FileHashes,
        torrent: TorrentArtifact,
        acquisition: AcquisitionEvidence,
    ) -> PublicationBundle: ...


class OutboxPublisher:
    """Persist the future Index request without making a network request."""

    def __init__(self, outbox_dir: Path):
        self.outbox_dir = outbox_dir

    def publish(
        self,
        release: Release,
        hashes: FileHashes,
        torrent: TorrentArtifact,
        acquisition: AcquisitionEvidence,
    ) -> PublicationBundle:
        destination = self.outbox_dir / release.id / release.sd_hash
        manifest_path = destination / "manifest.json"
        manifest = {
            "schema": "guncad-mirror-publication-v1",
            "status": "awaiting-index",
            "release": {
                "id": release.id,
                "name": release.name,
                "channel_handle": release.channel_handle,
                "url": release.url,
                "url_lbry": release.url_lbry,
            },
            "lbry": {
                "sd_hash": release.sd_hash,
                "claimed_sha384": release.sha384,
            },
            "acquisition": {
                "transport": acquisition.transport,
                "source_url": acquisition.source_url,
                "lbry_failure": acquisition.lbry_failure,
            },
            "artifact": {
                "file_name": torrent.file_path.name,
                "size": hashes.size,
                "sha384": hashes.sha384,
                "sha256": hashes.sha256,
            },
            "torrent": {
                "file_name": torrent.file_path.name,
                "piece_length": torrent.piece_length,
                "piece_count": torrent.piece_count,
                "btih": torrent.info_hash,
                "sha256": torrent.torrent_sha256,
                "magnet_uri": torrent.magnet_uri,
                "trackers": list(torrent.trackers),
            },
        }
        _atomic_json(manifest_path, manifest)
        return PublicationBundle(
            release=release,
            hashes=hashes,
            torrent=torrent,
            manifest_path=manifest_path,
        )


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temp:
        temporary_path = Path(temp.name)
        temp.write(content)
        temp.flush()
        os.fsync(temp.fileno())
    temporary_path.replace(path)
