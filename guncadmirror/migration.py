from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Any

from .models import (
    AcquisitionEvidence,
    AcquisitionTransport,
    FileHashes,
    JobState,
    Release,
    TorrentArtifact,
)
from .publisher import OutboxPublisher
from .qbittorrent import QBitClient
from .settings import Settings
from .state import JobStore
from .torrent import create_torrent, parse_torrent

logger = logging.getLogger("guncad-mirror.migration")

HEX_CLAIM_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


@dataclass(slots=True)
class MigrationConfig:
    data_dir: Path
    bootstrap_zip: Path | None = None
    target_prefix: Path = Path("/data")
    dry_run: bool = False
    verify_hashes: bool = False
    generate_missing_torrents: bool = True
    max_items: int | None = None
    detect_faulty_folders: bool = True
    sqlite_nolock: bool = False
    staging_db: bool = True
    logger: logging.Logger = logger


@dataclass(slots=True)
class MigrationStats:
    total_local_streams: int = 0
    matched_bootstrap: int = 0
    delisted_or_unindexed: int = 0
    torrents_extracted: int = 0
    torrents_generated: int = 0
    skipped_existing: int = 0
    missing_files: int = 0
    faulty_stubs_ignored: int = 0
    errors: int = 0
    total_bytes: int = 0
    delisted_releases: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "total_local_streams": self.total_local_streams,
            "matched_bootstrap": self.matched_bootstrap,
            "delisted_or_unindexed": self.delisted_or_unindexed,
            "torrents_extracted": self.torrents_extracted,
            "torrents_generated": self.torrents_generated,
            "skipped_existing": self.skipped_existing,
            "missing_files": self.missing_files,
            "faulty_stubs_ignored": self.faulty_stubs_ignored,
            "errors": self.errors,
            "total_bytes": self.total_bytes,
            "delisted_sample": self.delisted_releases[:20],
            "issues": self.issues[:100],
        }


def decode_hex_string(value: str) -> str:
    """Decode a hex string stored in legacy lbrynet sqlite if hex-encoded."""
    if not isinstance(value, str):
        return str(value)
    clean = value.strip("'\"")
    if (
        len(clean) >= 2
        and len(clean) % 2 == 0
        and all(c in "0123456789abcdefABCDEF" for c in clean)
    ):
        try:
            decoded = bytes.fromhex(clean).decode("utf-8")
            if all(c.isprintable() or c in "\r\n\t " for c in decoded):
                return decoded
        except Exception:
            pass
    return clean


def load_bootstrap_index(
    zip_path: Path,
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """
    Load guncad-index-torrents.zip manifest.json.
    Returns a dict mapping sd_hash -> (artifact_dict, release_dict).
    """
    if not zip_path.is_file():
        raise FileNotFoundError(f"Bootstrap zip file not found: {zip_path}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        with zf.open("manifest.json") as mf:
            data = json.load(mf)

    sd_index: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for artifact in data.get("artifacts", []):
        for release in artifact.get("releases", []):
            sd = release.get("sd_hash")
            if sd and isinstance(sd, str):
                sd_index[sd.lower()] = (artifact, release)
    return sd_index


def read_v1_lbrynet_db(sqlite_path: Path) -> list[dict[str, Any]]:
    """
    Read all streams from legacy lbrynet.sqlite where saved_file=1.
    """
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"lbrynet database not found: {sqlite_path}")

    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT f.file_name, f.download_directory, s.sd_hash, s.suggested_filename, f.saved_file, f.status
            FROM file f JOIN stream s USING (stream_hash)
            WHERE f.saved_file = 1
            """
        )
        records: list[dict[str, Any]] = []
        for row in cursor.fetchall():
            fn = decode_hex_string(row["file_name"] or row["suggested_filename"])
            dd = decode_hex_string(row["download_directory"] or "")
            records.append(
                {
                    "file_name": fn,
                    "download_directory": dd,
                    "sd_hash": row["sd_hash"].lower(),
                    "status": row["status"],
                }
            )
        return records
    finally:
        conn.close()


def read_v1_meta_json(directory: Path) -> dict[str, Any] | None:
    meta_path = directory / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def synthesize_v2_payload(
    *,
    release_id: str,
    name: str,
    channel_handle: str,
    sd_hash: str,
    size: int | None,
    checksum: str | None,
    url: str | None = None,
    url_lbry: str | None = None,
    channel_name: str | None = None,
    popularity: float = 1.0,
    lbry_only: bool = False,
) -> dict[str, Any]:
    """
    Construct an API v2 release payload that strictly validates under Release.from_api().
    """
    links: list[dict[str, str]] = []
    if url:
        links.append({"url": url})
    if url_lbry:
        links.append({"url": url_lbry})
    else:
        links.append({"url": f"lbry://{release_id}"})

    return {
        "id": release_id,
        "name": name,
        "channel": {
            "handle": channel_handle,
            "name": channel_name or channel_handle.split(":")[0],
        },
        "origin": {
            "platform": "lbry",
            "external_id": release_id,
            "size": size,
            "checksum": checksum,
            "popularity": popularity,
            "extra": {
                "sd_hash": sd_hash,
                "lbry_only": lbry_only,
            },
            "links": links,
        },
    }


def compute_hashes(path: Path) -> FileHashes:
    sha384_hasher = hashlib.sha384()
    sha256_hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            sha384_hasher.update(chunk)
            sha256_hasher.update(chunk)
            size += len(chunk)
    return FileHashes(
        size=size,
        sha384=sha384_hasher.hexdigest(),
        sha256=sha256_hasher.hexdigest(),
    )


def atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="wb",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as temp:
        temp.write(data)
        temp.flush()
        os.fsync(temp.fileno())
        temp_path = Path(temp.name)
    temp_path.replace(target)


class V1MigrationRunner:
    def __init__(self, config: MigrationConfig):
        self.config = config
        self.stats = MigrationStats()
        self.data_dir = config.data_dir.resolve()
        self.target_prefix = config.target_prefix
        self.logger = config.logger

        # Setup paths
        self.lbry_db_path = self.data_dir / "lbry" / "lbrynet" / "lbrynet.sqlite"
        self.mirror_dir = self.data_dir / "mirror"
        self.outbox_dir = self.data_dir / "outbox"
        self.releases_dir = self.data_dir / "releases"
        self.state_db_path = self.data_dir / "mirror-state.sqlite3"

        self.local_state_db: Path | None = None
        if not self.config.dry_run:
            if self.config.staging_db:
                self.local_state_db = (
                    Path(tempfile.gettempdir())
                    / f"mirror-state-migration-{os.getpid()}-{int(time.time())}.sqlite3"
                )
                if (
                    self.state_db_path.is_file()
                    and self.state_db_path.stat().st_size > 0
                ):
                    try:
                        shutil.copy2(self.state_db_path, self.local_state_db)
                    except OSError:
                        pass
                self.store = JobStore(self.local_state_db)
            else:
                self.store = JobStore(
                    self.state_db_path, nolock=self.config.sqlite_nolock
                )
        else:
            self.store = None
        self.publisher = OutboxPublisher(self.outbox_dir)

    def run(self) -> MigrationStats:
        self.logger.info("Starting GunCAD v1 -> v2 Migration Runner")
        self.logger.info("Data directory: %s", self.data_dir)
        self.logger.info("Target prefix for SQLite records: %s", self.target_prefix)
        self.logger.info("Dry run: %s", self.config.dry_run)

        # 1. Load bootstrap ZIP if available
        bootstrap_index: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        zip_obj: zipfile.ZipFile | None = None
        if self.config.bootstrap_zip and self.config.bootstrap_zip.is_file():
            self.logger.info(
                "Loading bootstrap index from %s...", self.config.bootstrap_zip
            )
            bootstrap_index = load_bootstrap_index(self.config.bootstrap_zip)
            zip_obj = zipfile.ZipFile(self.config.bootstrap_zip, "r")
            self.logger.info(
                "Bootstrap index loaded with %d artifacts", len(bootstrap_index)
            )

        # 2. Read legacy lbrynet database
        self.logger.info("Reading local stream records from %s...", self.lbry_db_path)
        stream_records = read_v1_lbrynet_db(self.lbry_db_path)
        self.stats.total_local_streams = len(stream_records)
        self.logger.info(
            "Discovered %d saved stream records in lbrynet.sqlite", len(stream_records)
        )

        # 3. Detect faulty empty stubs if requested
        if self.config.detect_faulty_folders and self.mirror_dir.is_dir():
            self._scan_and_record_faulty_stubs()

        if self.config.max_items:
            stream_records = stream_records[: self.config.max_items]
            self.logger.info(
                "Limiting migration to first %d items", len(stream_records)
            )

        # 4. Process each stream record
        db_conn = None
        if not self.config.dry_run and self.store is not None:
            db_conn = self.store._connect()

        try:
            for index, item in enumerate(stream_records, start=1):
                if index % 500 == 0 or index == len(stream_records):
                    self.logger.info(
                        "Processing record %d/%d (matched bootstrap: %d, delisted/unindexed: %d, generated: %d, missing: %d)...",
                        index,
                        len(stream_records),
                        self.stats.matched_bootstrap,
                        self.stats.delisted_or_unindexed,
                        self.stats.torrents_generated,
                        self.stats.missing_files,
                    )
                try:
                    self._migrate_single_stream(item, bootstrap_index, zip_obj, db_conn)
                    if db_conn is not None and index % 100 == 0:
                        db_conn.commit()
                except Exception as e:
                    self.stats.errors += 1
                    self.logger.exception(
                        "Error migrating stream %s: %s", item["sd_hash"][:12], e
                    )
                    self.stats.issues.append(
                        f"{item['sd_hash']}: {type(e).__name__}: {e}"
                    )

            if db_conn is not None:
                db_conn.commit()
        finally:
            if db_conn is not None:
                db_conn.close()

        # Finalize and deploy staged database
        if (
            not self.config.dry_run
            and self.local_state_db is not None
            and self.local_state_db.is_file()
        ):
            self.logger.info("Finalizing staged SQLite database with checkpoint...")
            with closing(sqlite3.connect(self.local_state_db)) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.logger.info(
                "Deploying finalized SQLite database to %s...", self.state_db_path
            )
            atomic_write(self.state_db_path, self.local_state_db.read_bytes())
            self.logger.info(
                "Database deployment to %s completed successfully", self.state_db_path
            )

        if zip_obj:
            zip_obj.close()

        # 5. Save summary
        summary = self.stats.summary()
        self.logger.info("Migration finished: %s", json.dumps(summary, indent=2))
        if not self.config.dry_run:
            reports_dir = self.data_dir / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            report_file = reports_dir / "migration-summary.json"
            atomic_write(
                report_file, (json.dumps(summary, indent=2) + "\n").encode("utf-8")
            )
            self.logger.info("Wrote migration summary report to %s", report_file)

        return self.stats

    def _scan_and_record_faulty_stubs(self) -> None:
        """Scan author directories for faulty 40-character hex directories with no payload."""
        faulty_stubs = 0
        try:
            with os.scandir(self.mirror_dir) as authors:
                for author in authors:
                    if not author.is_dir():
                        continue
                    try:
                        with os.scandir(author.path) as releases:
                            for rel in releases:
                                if not rel.is_dir():
                                    continue
                                if HEX_CLAIM_RE.fullmatch(rel.name):
                                    # Check if empty or only meta.json
                                    has_payload = any(
                                        f.is_file() and f.name != "meta.json"
                                        for f in os.scandir(rel.path)
                                    )
                                    if not has_payload:
                                        faulty_stubs += 1
                    except OSError:
                        pass
        except OSError:
            pass
        self.stats.faulty_stubs_ignored = faulty_stubs
        if faulty_stubs:
            self.logger.info(
                "Identified %d faulty metadata-only stubs in long-numbered folders; bypassed safely",
                faulty_stubs,
            )

    def _migrate_single_stream(
        self,
        item: dict[str, Any],
        bootstrap_index: dict[str, tuple[dict[str, Any], dict[str, Any]]],
        zip_obj: zipfile.ZipFile | None,
        db_conn: sqlite3.Connection | None = None,
    ) -> None:
        sd_hash = item["sd_hash"]
        file_name = item["file_name"]
        download_directory = item["download_directory"]

        # Resolve local payload path
        rel_subpath = download_directory.lstrip("/")
        if rel_subpath.startswith("data/"):
            rel_subpath = rel_subpath[5:]
        local_dir = self.data_dir / rel_subpath
        local_payload_path = local_dir / file_name

        if not local_payload_path.is_file():
            self.stats.missing_files += 1
            self.logger.debug("Payload missing: %s", local_payload_path)
            self.stats.issues.append(f"missing payload: {local_payload_path}")
            return

        payload_size = local_payload_path.stat().st_size
        self.stats.total_bytes += payload_size

        # Resolve target container paths for SQLite ledger
        target_payload_path = self.target_prefix / rel_subpath / file_name

        matched_bootstrap = sd_hash in bootstrap_index
        meta_json = read_v1_meta_json(local_dir)

        release_id: str | None = None
        release_name: str | None = None
        channel_handle: str | None = None
        sha384: str | None = None
        btih: str | None = None
        magnet_uri: str | None = None
        torrent_bytes: bytes | None = None

        if matched_bootstrap:
            self.stats.matched_bootstrap += 1
            artifact, rel_info = bootstrap_index[sd_hash]
            release_id = (
                rel_info.get("id")
                or rel_info.get("source_release_id")
                or artifact.get("source_release_id")
            )
            release_name = rel_info.get("name") or local_payload_path.stem
            channel_handle = rel_info.get("channel") or "Unknown"
            sha384 = artifact.get("sha384")
            btih = artifact.get("btih")
            magnet_uri = artifact.get("magnet_uri")

            torrent_in_zip = artifact.get("torrent")
            if zip_obj and torrent_in_zip and torrent_in_zip in zip_obj.namelist():
                torrent_bytes = zip_obj.read(torrent_in_zip)

        elif meta_json:
            self.stats.delisted_or_unindexed += 1
            release_id = meta_json.get("id")
            release_name = meta_json.get("name") or local_payload_path.stem
            channel_handle = meta_json.get("channel", {}).get("handle", "Unknown")
            btih = None
            magnet_uri = None
            if "torrent" in meta_json and isinstance(meta_json["torrent"], dict):
                btih = meta_json["torrent"].get("btih")
                magnet_uri = meta_json["torrent"].get("magnet_uri")

            self.stats.delisted_releases.append(
                {
                    "release_id": release_id,
                    "name": release_name,
                    "channel": channel_handle,
                    "sd_hash": sd_hash,
                    "size": payload_size,
                    "file_path": str(target_payload_path),
                    "btih": btih,
                    "magnet_uri": magnet_uri,
                }
            )

        if not release_id or not channel_handle:
            self.stats.errors += 1
            self.stats.issues.append(f"No release ID or channel for sd_hash {sd_hash}")
            return

        # Check existing job in state db
        if db_conn is not None:
            cursor = db_conn.execute(
                "SELECT state FROM jobs WHERE release_id=? AND sd_hash=?",
                (release_id, sd_hash),
            )
            row = cursor.fetchone()
            if row is not None and row[0] == JobState.AWAITING_INDEX.value:
                self.stats.skipped_existing += 1
                return
        elif self.store is not None:
            try:
                existing_job = self.store.get(release_id, sd_hash)
                if existing_job.state is JobState.AWAITING_INDEX:
                    self.stats.skipped_existing += 1
                    return
            except KeyError:
                pass

        # Destination paths in outbox
        local_outbox_dest = self.outbox_dir / release_id / sd_hash
        target_outbox_dest = self.target_prefix / "outbox" / release_id / sd_hash

        # Check if outbox package already exists on disk
        existing_manifest_path = local_outbox_dest / "manifest.json"
        has_existing_outbox = False
        if sha384:
            torrent_filename = f"{sha384}.torrent"
            local_torrent_path = local_outbox_dest / torrent_filename
            target_torrent_path = target_outbox_dest / torrent_filename
            has_existing_outbox = (
                existing_manifest_path.is_file() and local_torrent_path.is_file()
            )
        else:
            torrent_filename = None
            local_torrent_path = None
            target_torrent_path = None

        # Compute or verify hashes
        hashes: FileHashes
        if has_existing_outbox:
            try:
                manifest_doc = json.loads(
                    existing_manifest_path.read_text(encoding="utf-8")
                )
                manifest_sha256 = manifest_doc.get("artifact", {}).get("sha256")
            except Exception:
                manifest_sha256 = None
            if manifest_sha256:
                hashes = FileHashes(
                    size=payload_size, sha384=sha384, sha256=manifest_sha256
                )
            else:
                hashes = compute_hashes(local_payload_path)
        elif self.config.verify_hashes or not sha384:
            hashes = compute_hashes(local_payload_path)
            if sha384 and hashes.sha384 != sha384:
                raise ValueError(
                    f"SHA-384 mismatch for {local_payload_path}: claimed {sha384}, got {hashes.sha384}"
                )
            sha384 = hashes.sha384
        else:
            hashes = FileHashes(
                size=payload_size,
                sha384=sha384,
                sha256=hashlib.sha256(
                    open(local_payload_path, "rb").read(1024 * 1024)
                ).hexdigest(),
            )

        if not sha384:
            sha384 = hashes.sha384
        if local_torrent_path is None:
            torrent_filename = f"{sha384}.torrent"
            local_torrent_path = local_outbox_dest / torrent_filename
            target_torrent_path = target_outbox_dest / torrent_filename

        torrent_artifact: TorrentArtifact

        if has_existing_outbox:
            torrent_bytes = local_torrent_path.read_bytes()
            parsed = parse_torrent(torrent_bytes)
            torrent_artifact = TorrentArtifact(
                file_path=target_payload_path,
                torrent_path=target_torrent_path,
                piece_length=parsed.piece_length,
                piece_count=parsed.piece_count,
                info_hash=parsed.info_hash,
                torrent_sha256=hashlib.sha256(torrent_bytes).hexdigest(),
                magnet_uri=parsed.magnet_uri,
                trackers=(),
            )
            if matched_bootstrap:
                self.stats.torrents_extracted += 1
            else:
                self.stats.torrents_generated += 1
        else:
            if torrent_bytes is not None:
                parsed = parse_torrent(torrent_bytes)
                if (
                    parsed.file_name != local_payload_path.name
                    or parsed.file_length != payload_size
                ):
                    torrent_bytes = None

            if torrent_bytes is not None:
                if not self.config.dry_run:
                    atomic_write(local_torrent_path, torrent_bytes)
                self.stats.torrents_extracted += 1
                torrent_artifact = TorrentArtifact(
                    file_path=target_payload_path,
                    torrent_path=target_torrent_path,
                    piece_length=parsed.piece_length,
                    piece_count=parsed.piece_count,
                    info_hash=parsed.info_hash,
                    torrent_sha256=hashlib.sha256(torrent_bytes).hexdigest(),
                    magnet_uri=parsed.magnet_uri,
                    trackers=(),
                )
            elif self.config.generate_missing_torrents:
                if not self.config.dry_run:
                    generated = create_torrent(
                        local_payload_path,
                        local_torrent_path,
                        piece_length=1048576,
                    )
                    torrent_bytes = local_torrent_path.read_bytes()
                    torrent_artifact = TorrentArtifact(
                        file_path=target_payload_path,
                        torrent_path=target_torrent_path,
                        piece_length=generated.piece_length,
                        piece_count=generated.piece_count,
                        info_hash=generated.info_hash,
                        torrent_sha256=generated.torrent_sha256,
                        magnet_uri=generated.magnet_uri,
                        trackers=generated.trackers,
                    )
                else:
                    torrent_artifact = TorrentArtifact(
                        file_path=target_payload_path,
                        torrent_path=target_torrent_path,
                        piece_length=1048576,
                        piece_count=(payload_size + 1048575) // 1048576,
                        info_hash="0" * 40,
                        torrent_sha256="0" * 64,
                        magnet_uri="",
                        trackers=(),
                    )
                self.stats.torrents_generated += 1
            else:
                self.stats.issues.append(
                    f"No torrent available for {sd_hash} (generate disabled)"
                )
                return

        # Build v2 Release model
        v2_raw = synthesize_v2_payload(
            release_id=release_id,
            name=release_name,
            channel_handle=channel_handle,
            sd_hash=sd_hash,
            size=payload_size,
            checksum=sha384,
            url=meta_json.get("url") if meta_json else None,
            url_lbry=meta_json.get("url_lbry") if meta_json else None,
        )
        release_obj = Release.from_api(v2_raw)

        if not self.config.dry_run and self.store is not None:
            if not has_existing_outbox:
                self.publisher.publish(
                    release_obj,
                    hashes,
                    torrent_artifact,
                    AcquisitionEvidence(transport=AcquisitionTransport.LBRY),
                )

            self.store.record_migrated(
                release_obj,
                file_path=target_payload_path,
                sha384=sha384,
                sha256=hashes.sha256,
                torrent=torrent_artifact,
                connection=db_conn,
            )


def relocate_qbit_seeds(
    settings: Settings,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    logger: logging.Logger = logger,
) -> dict[str, int]:
    """
    Relocate torrents in qBittorrent that have legacy or mismatched save paths
    (such as /downloads/mirror/...) to their canonical verified location (/downloads/releases/...)
    as recorded in the SQLite jobs ledger.
    """
    client = QBitClient(
        str(settings.qbittorrent_url),
        timeout=settings.qbittorrent_timeout,
        api_key=settings.qbittorrent_api_key,
        username=settings.qbittorrent_username,
        password=settings.qbittorrent_password,
    )
    logger.info("Connecting to qBittorrent at %s...", settings.qbittorrent_url)
    torrents_data = client._json("GET", "/api/v2/torrents/info")
    if not isinstance(torrents_data, list):
        raise RuntimeError("Failed to fetch torrent list from qBittorrent")

    logger.info("Discovered %d torrents in qBittorrent", len(torrents_data))

    db_path = settings.state_path
    if not db_path.is_file():
        raise FileNotFoundError(f"State database not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT info_hash, file_path, release_name FROM jobs WHERE file_path IS NOT NULL"
        )
        job_map: dict[str, tuple[str, str]] = {
            row[0].lower(): (row[1], row[2]) for row in cur.fetchall() if row[0]
        }

        stats = {
            "scanned": len(torrents_data),
            "relocated": 0,
            "skipped_canonical": 0,
            "skipped_missing": 0,
        }

        for item in torrents_data:
            if limit and stats["relocated"] >= limit:
                break
            info_hash = item.get("hash", "").lower()
            current_save = item.get("save_path", "")
            if not info_hash or info_hash not in job_map:
                stats["skipped_missing"] += 1
                continue

            file_path_str, release_name = job_map[info_hash]
            try:
                file_path = Path(file_path_str)
                rel = file_path.resolve().relative_to(settings.data_dir.resolve())
            except (ValueError, OSError):
                rel_parts = PurePosixPath(file_path_str).parts
                if len(rel_parts) > 2 and rel_parts[1] == "data":
                    rel = PurePosixPath(*rel_parts[2:])
                else:
                    stats["skipped_missing"] += 1
                    continue

            canonical_save = str(
                PurePosixPath(settings.qbittorrent_data_dir.as_posix()).joinpath(
                    *rel.parts[:-1]
                )
            )
            if PurePosixPath(current_save) == PurePosixPath(canonical_save):
                stats["skipped_canonical"] += 1
                continue

            local_payload = settings.data_dir.resolve() / rel
            if not local_payload.is_file():
                logger.warning(
                    "Payload missing on disk for %s at %s, skipping relocation",
                    release_name,
                    local_payload,
                )
                stats["skipped_missing"] += 1
                continue

            if not dry_run:
                client.set_location(info_hash, canonical_save)
                cur.execute(
                    """
                    UPDATE jobs SET
                        seeding_state='pending',
                        seeding_attempts=0,
                        seeding_next_attempt_at=0,
                        seeding_error_code=NULL,
                        seeding_error=NULL
                    WHERE info_hash=? AND seeding_error_code='content_path_conflict'
                    """,
                    (info_hash,),
                )
            stats["relocated"] += 1
            if stats["relocated"] % 500 == 0:
                logger.info("Relocated %d torrents...", stats["relocated"])
                if not dry_run:
                    conn.commit()

        if not dry_run:
            conn.commit()

        logger.info("Relocation complete: %s", stats)
        return stats
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m guncadmirror.migration",
        description="Migrate legacy v1 GunCAD Mirror data to API v2 outbox and SQLite state.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Path to the root v1/v2 data directory (containing lbry/, mirror/, etc.)",
    )
    parser.add_argument(
        "--bootstrap-zip",
        type=Path,
        default=None,
        help="Path to guncad-index-torrents.zip containing canonical Index torrents",
    )
    parser.add_argument(
        "--target-prefix",
        type=Path,
        default=Path("/data"),
        help="Container data path prefix to write into SQLite (default: /data)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and calculate stats without writing files or mutating SQLite",
    )
    parser.add_argument(
        "--verify-hashes",
        action="store_true",
        help="Recompute streaming SHA-384 and SHA-256 over full payloads",
    )
    parser.add_argument(
        "--no-generate-missing",
        action="store_true",
        help="Do not generate torrents for releases absent from bootstrap ZIP",
    )
    parser.add_argument(
        "--no-detect-faulty",
        action="store_true",
        help="Skip scanning for faulty metadata stubs",
    )
    parser.add_argument(
        "--relocate-seeds",
        action="store_true",
        help="Relocate legacy or mismatched seeds in qBittorrent to canonical storage locations",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="Limit migration to the first N items for testing",
    )
    parser.add_argument(
        "--nolock",
        action="store_true",
        help="Enable SQLite ?nolock=1 mode when writing to state database (useful for CIFS/SMB mounts)",
    )
    parser.add_argument(
        "--no-staging-db",
        action="store_true",
        help="Write directly to target database rather than using a local staging SQLite file",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose DEBUG logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if args.relocate_seeds:
        settings = Settings.from_env()
        if args.data_dir:
            settings = Settings(
                **{
                    k: getattr(settings, k)
                    for k in settings.__dataclass_fields__
                    if k != "data_dir"
                },
                data_dir=args.data_dir.resolve(),
            )
        relocate_qbit_seeds(
            settings,
            dry_run=args.dry_run,
            limit=args.max_items,
            logger=logger,
        )
        return

    if not args.data_dir:
        parser.error("--data-dir is required when not using --relocate-seeds")

    config = MigrationConfig(
        data_dir=args.data_dir,
        bootstrap_zip=args.bootstrap_zip,
        target_prefix=args.target_prefix,
        dry_run=args.dry_run,
        verify_hashes=args.verify_hashes,
        generate_missing_torrents=not args.no_generate_missing,
        detect_faulty_folders=not args.no_detect_faulty,
        sqlite_nolock=args.nolock,
        staging_db=not args.no_staging_db,
        max_items=args.max_items,
    )

    runner = V1MigrationRunner(config)
    runner.run()


if __name__ == "__main__":
    main()
