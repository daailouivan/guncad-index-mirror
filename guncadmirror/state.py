from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any

from .models import (
    SUPPORTED_PLATFORMS,
    JobState,
    PublicationState,
    Release,
    SeedingState,
    TorrentArtifact,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    release_id TEXT NOT NULL,
    sd_hash TEXT NOT NULL,
    release_json TEXT NOT NULL,
    release_name TEXT NOT NULL DEFAULT '',
    channel_handle TEXT NOT NULL DEFAULT '',
    release_slug TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT 'lbry',
    payload_size INTEGER,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    file_path TEXT,
    sha384 TEXT,
    sha256 TEXT,
    torrent_path TEXT,
    info_hash TEXT,
    magnet_uri TEXT,
    last_error TEXT,
    exclusion_reason TEXT,
    seeding_state TEXT NOT NULL DEFAULT 'pending',
    seeding_attempts INTEGER NOT NULL DEFAULT 0,
    seeding_next_attempt_at REAL NOT NULL DEFAULT 0,
    seeding_error_code TEXT,
    seeding_error TEXT,
    seeding_client TEXT,
    seeding_client_version TEXT,
    seeding_observed_state TEXT,
    seeding_content_path TEXT,
    seeding_dht_nodes INTEGER,
    seeding_working_trackers INTEGER,
    seeding_checked_at REAL,
    seeding_updated_at REAL,
    publication_state TEXT NOT NULL DEFAULT 'pending',
    publication_attempts INTEGER NOT NULL DEFAULT 0,
    publication_next_attempt_at REAL NOT NULL DEFAULT 0,
    publication_outcome TEXT,
    publication_canonical INTEGER,
    canonical_sha384 TEXT,
    canonical_btih TEXT,
    canonical_torrent_url TEXT,
    canonical_magnet_uri TEXT,
    winning_release_id TEXT,
    publication_error_code TEXT,
    publication_error TEXT,
    publication_updated_at REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (release_id, sd_hash)
);

CREATE TABLE IF NOT EXISTS tracker_policy_cache (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    endpoint TEXT NOT NULL,
    etag TEXT NOT NULL,
    document BLOB NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mirror_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class Job:
    release_id: str
    sd_hash: str
    state: JobState
    attempts: int
    next_attempt_at: float
    file_path: Path | None
    sha384: str | None
    sha256: str | None
    torrent_path: Path | None
    info_hash: str | None
    magnet_uri: str | None
    last_error: str | None
    exclusion_reason: str | None
    seeding_state: SeedingState
    seeding_attempts: int
    seeding_next_attempt_at: float
    seeding_error_code: str | None
    seeding_error: str | None
    seeding_client: str | None
    seeding_client_version: str | None
    seeding_observed_state: str | None
    seeding_content_path: str | None
    seeding_dht_nodes: int | None
    seeding_working_trackers: int | None
    seeding_checked_at: float | None
    seeding_updated_at: float | None
    publication_state: PublicationState
    publication_attempts: int
    publication_next_attempt_at: float
    publication_outcome: str | None
    publication_canonical: bool | None
    canonical_sha384: str | None
    canonical_btih: str | None
    canonical_torrent_url: str | None
    canonical_magnet_uri: str | None
    winning_release_id: str | None
    publication_error_code: str | None
    publication_error: str | None
    publication_updated_at: float | None
    platform: str = "lbry"
    payload_size: int | None = None


@dataclass(frozen=True, slots=True)
class PublicationCandidate:
    release: Release
    job: Job


@dataclass(frozen=True, slots=True)
class SeedingCandidate:
    release: Release
    job: Job


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    release_id: str
    sd_hash: str
    name: str
    channel_handle: str
    slug: str
    file_name: str
    size: int | None
    magnet_uri: str | None
    seeding_state: SeedingState
    seeding_observed_state: str | None
    seeding_dht_nodes: int | None
    seeding_working_trackers: int | None
    seeding_error_code: str | None
    publication_state: PublicationState
    canonical_magnet_uri: str | None
    canonical_torrent_url: str | None
    platform: str = "lbry"


PLATFORMS_DISPLAY_ORDER = ("lbry", "printables", "github", "http", "torrent")
PLATFORM_DISPLAY_NAMES = {
    "lbry": "LBRY / Odysee",
    "printables": "Printables",
    "github": "GitHub",
    "http": "Direct HTTP",
    "torrent": "BitTorrent Swarm",
}


@dataclass(frozen=True, slots=True)
class TrackerPolicyCache:
    endpoint: str
    etag: str
    document: bytes
    updated_at: float


class JobStore:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time,
        nolock: bool = False,
    ):
        self.path = path
        self.clock = clock
        self.nolock = nolock
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
            }
            if "exclusion_reason" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN exclusion_reason TEXT")
            archive_columns = {
                "release_name": "TEXT NOT NULL DEFAULT ''",
                "channel_handle": "TEXT NOT NULL DEFAULT ''",
                "release_slug": "TEXT NOT NULL DEFAULT ''",
                "platform": "TEXT NOT NULL DEFAULT ''",
                "payload_size": "INTEGER",
            }
            for column, definition in archive_columns.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE jobs ADD COLUMN {column} {definition}"
                    )
            seeding_columns = {
                "seeding_state": "TEXT NOT NULL DEFAULT 'pending'",
                "seeding_attempts": "INTEGER NOT NULL DEFAULT 0",
                "seeding_next_attempt_at": "REAL NOT NULL DEFAULT 0",
                "seeding_error_code": "TEXT",
                "seeding_error": "TEXT",
                "seeding_client": "TEXT",
                "seeding_client_version": "TEXT",
                "seeding_observed_state": "TEXT",
                "seeding_content_path": "TEXT",
                "seeding_dht_nodes": "INTEGER",
                "seeding_working_trackers": "INTEGER",
                "seeding_checked_at": "REAL",
                "seeding_updated_at": "REAL",
            }
            for column, definition in seeding_columns.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE jobs ADD COLUMN {column} {definition}"
                    )
            publication_columns = {
                "publication_state": "TEXT NOT NULL DEFAULT 'pending'",
                "publication_attempts": "INTEGER NOT NULL DEFAULT 0",
                "publication_next_attempt_at": "REAL NOT NULL DEFAULT 0",
                "publication_outcome": "TEXT",
                "publication_canonical": "INTEGER",
                "canonical_sha384": "TEXT",
                "canonical_btih": "TEXT",
                "canonical_torrent_url": "TEXT",
                "canonical_magnet_uri": "TEXT",
                "winning_release_id": "TEXT",
                "publication_error_code": "TEXT",
                "publication_error": "TEXT",
                "publication_updated_at": "REAL",
            }
            for column, definition in publication_columns.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE jobs ADD COLUMN {column} {definition}"
                    )
            self._backfill_archive_fields(connection)
            self._backfill_platform_and_size(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS jobs_archive_listing
                ON jobs (
                    state,
                    channel_handle COLLATE NOCASE,
                    release_name COLLATE NOCASE,
                    release_id,
                    sd_hash
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS jobs_seeding_queue
                ON jobs (
                    state,
                    seeding_state,
                    seeding_next_attempt_at,
                    release_id,
                    sd_hash
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS jobs_publication_queue
                ON jobs (
                    state,
                    publication_state,
                    publication_next_attempt_at,
                    sha384,
                    release_id,
                    sd_hash
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS jobs_platform_state
                ON jobs (
                    platform,
                    state,
                    seeding_state,
                    publication_state
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        if self.nolock:
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?nolock=1", uri=True, timeout=60
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 60000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            return connection

        connection = sqlite3.connect(self.path, timeout=60)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 60000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def get_setting(self, key: str, default: str = "") -> str:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "SELECT value FROM mirror_settings WHERE key = ?", (key,)
            )
            row = cursor.fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        now = self.clock()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO mirror_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now),
            )

    def retry_failed_jobs(self, *, platform: str | None = None) -> int:
        now = self.clock()
        query = (
            "UPDATE jobs SET state=?, attempts=0, next_attempt_at=0, "
            "last_error=NULL, updated_at=? WHERE state=?"
        )
        params: list[Any] = [JobState.PENDING, now, JobState.FAILED]
        if platform:
            query += " AND platform=?"
            params.append(platform)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(query, tuple(params))
            return cursor.rowcount

    def register(self, release: Release) -> Job:
        now = self.clock()
        release_slug = _release_slug(release)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    release_id, sd_hash, release_json, release_name,
                    channel_handle, release_slug, platform, payload_size,
                    state, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(release_id, sd_hash) DO UPDATE SET
                    release_json=excluded.release_json,
                    release_name=excluded.release_name,
                    channel_handle=excluded.channel_handle,
                    release_slug=excluded.release_slug,
                    platform=excluded.platform,
                    payload_size=COALESCE(jobs.payload_size, excluded.payload_size),
                    updated_at=excluded.updated_at
                """,
                (
                    release.id,
                    release.sd_hash,
                    release.to_json(),
                    release.name,
                    release.channel_handle,
                    release_slug,
                    release.platform,
                    release.size,
                    JobState.PENDING,
                    now,
                ),
            )
        return self.get(release.id, release.sd_hash)

    def get(self, release_id: str, sd_hash: str) -> Job:
        with (
            closing(self._connect()) as connection,
            closing(
                connection.execute(
                    "SELECT * FROM jobs WHERE release_id=? AND sd_hash=?",
                    (release_id, sd_hash),
                )
            ) as cursor,
        ):
            row = cursor.fetchone()
        if row is None:
            raise KeyError((release_id, sd_hash))
        return _job_from_row(row)

    def ready_for_attempt(self, job: Job) -> bool:
        return (
            job.state not in {JobState.AWAITING_INDEX}
            and job.next_attempt_at <= self.clock()
        )

    def pending_releases(
        self,
        platform: str | None = None,
        limit: int = 50,
    ) -> list[Release]:
        now = self.clock()
        clauses = ["state = ?", "next_attempt_at <= ?"]
        parameters: list[Any] = [JobState.PENDING, now]
        if platform:
            clauses.append("platform = ?")
            parameters.append(platform.lower())
        where = " AND ".join(clauses)
        parameters.append(max(1, limit))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT release_json FROM jobs
                WHERE {where}
                ORDER BY updated_at ASC, release_id ASC
                LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
        releases: list[Release] = []
        for row in rows:
            try:
                releases.append(Release.from_api(json.loads(row["release_json"])))
            except Exception:
                continue
        return releases

    def start_attempt(self, release: Release) -> Job:
        now = self.clock()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE jobs SET
                    state=?, attempts=attempts + 1, next_attempt_at=0,
                    last_error=NULL, exclusion_reason=NULL, updated_at=?
                WHERE release_id=? AND sd_hash=?
                """,
                (JobState.ACQUIRING, now, release.id, release.sd_hash),
            )
        return self.get(release.id, release.sd_hash)

    def mark_verified(
        self, release: Release, *, file_path: Path, sha384: str, sha256: str
    ) -> Job:
        try:
            file_size: int | None = file_path.stat().st_size
        except OSError:
            file_size = release.size
        self._update(
            release,
            state=JobState.VERIFIED,
            file_path=str(file_path),
            payload_size=file_size,
            sha384=sha384,
            sha256=sha256,
            last_error=None,
            exclusion_reason=None,
        )
        return self.get(release.id, release.sd_hash)

    def mark_awaiting_index(self, release: Release, torrent: TorrentArtifact) -> Job:
        self._update(
            release,
            state=JobState.AWAITING_INDEX,
            torrent_path=str(torrent.torrent_path),
            info_hash=torrent.info_hash,
            magnet_uri=torrent.magnet_uri,
            seeding_state=SeedingState.PENDING,
            seeding_attempts=0,
            seeding_next_attempt_at=0,
            seeding_error_code=None,
            seeding_error=None,
            seeding_client=None,
            seeding_client_version=None,
            seeding_observed_state=None,
            seeding_content_path=None,
            seeding_dht_nodes=None,
            seeding_working_trackers=None,
            seeding_checked_at=None,
            seeding_updated_at=self.clock(),
            last_error=None,
            exclusion_reason=None,
        )
        return self.get(release.id, release.sd_hash)

    def record_migrated(
        self,
        release: Release,
        *,
        file_path: Path,
        sha384: str,
        sha256: str,
        torrent: TorrentArtifact,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        now = self.clock()
        release_slug = _release_slug(release)
        try:
            file_size: int | None = file_path.stat().st_size
        except OSError:
            file_size = release.size
        query = """
            INSERT INTO jobs (
                release_id, sd_hash, release_json, release_name,
                channel_handle, release_slug, platform, payload_size,
                state,
                file_path, sha384, sha256,
                torrent_path, info_hash, magnet_uri,
                seeding_state, seeding_attempts, seeding_next_attempt_at, seeding_updated_at,
                publication_state, publication_attempts, publication_next_attempt_at,
                updated_at
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?
            )
            ON CONFLICT(release_id, sd_hash) DO UPDATE SET
                release_json=excluded.release_json,
                release_name=excluded.release_name,
                channel_handle=excluded.channel_handle,
                release_slug=excluded.release_slug,
                platform=excluded.platform,
                payload_size=COALESCE(excluded.payload_size, jobs.payload_size),
                state=excluded.state,
                file_path=excluded.file_path,
                sha384=excluded.sha384,
                sha256=excluded.sha256,
                torrent_path=excluded.torrent_path,
                info_hash=excluded.info_hash,
                magnet_uri=excluded.magnet_uri,
                seeding_state=excluded.seeding_state,
                updated_at=excluded.updated_at
        """
        params = (
            release.id,
            release.sd_hash,
            release.to_json(),
            release.name,
            release.channel_handle,
            release_slug,
            release.platform,
            file_size,
            JobState.AWAITING_INDEX,
            str(file_path),
            sha384,
            sha256,
            str(torrent.torrent_path),
            torrent.info_hash,
            torrent.magnet_uri,
            SeedingState.PENDING,
            0,
            0,
            now,
            PublicationState.PENDING,
            0,
            0,
            now,
        )
        if connection is not None:
            connection.execute(query, params)
        else:
            with closing(self._connect()) as conn, conn:
                conn.execute(query, params)

    def mark_excluded(self, release: Release, reason: str) -> Job:
        self._update(
            release,
            state=JobState.EXCLUDED,
            next_attempt_at=0,
            last_error=None,
            exclusion_reason=reason,
        )
        return self.get(release.id, release.sd_hash)

    def mark_failed(
        self, release: Release, error: Exception, *, retry_backoff: float
    ) -> Job:
        job = self.get(release.id, release.sd_hash)
        delay = retry_backoff * (2 ** max(job.attempts - 1, 0))
        self._update(
            release,
            state=JobState.FAILED,
            next_attempt_at=self.clock() + delay,
            last_error=f"{type(error).__name__}: {error}",
            exclusion_reason=None,
        )
        return self.get(release.id, release.sd_hash)

    def counts(self) -> dict[str, int]:
        with (
            closing(self._connect()) as connection,
            closing(
                connection.execute(
                    "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"
                )
            ) as cursor,
        ):
            rows = cursor.fetchall()
        return {row["state"]: row["count"] for row in rows}

    def publication_counts(self) -> dict[str, int]:
        with (
            closing(self._connect()) as connection,
            closing(
                connection.execute(
                    """
                    SELECT publication_state, COUNT(*) AS count
                    FROM jobs
                    WHERE state=?
                    GROUP BY publication_state
                    """,
                    (JobState.AWAITING_INDEX,),
                )
            ) as cursor,
        ):
            rows = cursor.fetchall()
        return {row["publication_state"]: row["count"] for row in rows}

    def seeding_counts(self) -> dict[str, int]:
        with (
            closing(self._connect()) as connection,
            closing(
                connection.execute(
                    """
                    SELECT seeding_state, COUNT(*) AS count
                    FROM jobs
                    WHERE state=?
                    GROUP BY seeding_state
                    """,
                    (JobState.AWAITING_INDEX,),
                )
            ) as cursor,
        ):
            rows = cursor.fetchall()
        return {row["seeding_state"]: row["count"] for row in rows}

    def platform_breakdown(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {
            plat: {
                "platform": plat,
                "display_name": PLATFORM_DISPLAY_NAMES.get(plat, plat.capitalize()),
                "total": 0,
                "pending": 0,
                "acquiring": 0,
                "verified": 0,
                "awaiting_index": 0,
                "seeding_green": 0,
                "published": 0,
                "failed": 0,
                "excluded": 0,
                "staged_bytes": 0,
                "total_bytes": 0,
            }
            for plat in PLATFORMS_DISPLAY_ORDER
        }
        with (
            closing(self._connect()) as connection,
            closing(
                connection.execute(
                    """
                    SELECT
                        COALESCE(NULLIF(platform, ''), 'lbry') AS plat,
                        COUNT(*) AS total,
                        SUM(CASE WHEN state = 'pending' THEN 1 ELSE 0 END) AS pending,
                        SUM(CASE WHEN state = 'acquiring' THEN 1 ELSE 0 END) AS acquiring,
                        SUM(CASE WHEN state = 'verified' THEN 1 ELSE 0 END) AS verified,
                        SUM(CASE WHEN state = 'awaiting_index' THEN 1 ELSE 0 END) AS awaiting_index,
                        SUM(CASE WHEN state = 'awaiting_index' AND seeding_state = 'green' THEN 1 ELSE 0 END) AS seeding_green,
                        SUM(CASE WHEN state = 'awaiting_index' AND publication_state IN ('published', 'duplicate') THEN 1 ELSE 0 END) AS published,
                        SUM(CASE WHEN state = 'failed' THEN 1 ELSE 0 END) AS failed,
                        SUM(CASE WHEN state = 'excluded' THEN 1 ELSE 0 END) AS excluded,
                        SUM(CASE WHEN state = 'awaiting_index' THEN COALESCE(payload_size, 0) ELSE 0 END) AS staged_bytes,
                        SUM(COALESCE(payload_size, 0)) AS total_bytes
                    FROM jobs
                    GROUP BY COALESCE(NULLIF(platform, ''), 'lbry')
                    """
                )
            ) as cursor,
        ):
            for row in cursor.fetchall():
                plat = row["plat"]
                if plat not in result:
                    result[plat] = {
                        "platform": plat,
                        "display_name": PLATFORM_DISPLAY_NAMES.get(
                            plat, plat.capitalize()
                        ),
                        "total": 0,
                        "pending": 0,
                        "acquiring": 0,
                        "verified": 0,
                        "awaiting_index": 0,
                        "seeding_green": 0,
                        "published": 0,
                        "failed": 0,
                        "excluded": 0,
                        "staged_bytes": 0,
                        "total_bytes": 0,
                    }
                item = result[plat]
                item["total"] = row["total"]
                item["pending"] = row["pending"]
                item["acquiring"] = row["acquiring"]
                item["verified"] = row["verified"]
                item["awaiting_index"] = row["awaiting_index"]
                item["seeding_green"] = row["seeding_green"]
                item["published"] = row["published"]
                item["failed"] = row["failed"]
                item["excluded"] = row["excluded"]
                item["staged_bytes"] = row["staged_bytes"] or 0
                item["total_bytes"] = row["total_bytes"] or 0
        return result

    def recover_interrupted_seeding(self) -> int:
        now = self.clock()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET
                    seeding_state=?,
                    seeding_next_attempt_at=0,
                    seeding_error_code='interrupted',
                    seeding_error='Mirror stopped during qBittorrent injection',
                    seeding_updated_at=?
                WHERE seeding_state=?
                """,
                (SeedingState.RETRYING, now, SeedingState.INJECTING),
            )
        return cursor.rowcount

    def seeding_candidates(self) -> list[SeedingCandidate]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE
                    state=? AND
                    seeding_state IN (?, ?, ?, ?) AND
                    file_path IS NOT NULL AND
                    torrent_path IS NOT NULL AND
                    info_hash IS NOT NULL
                ORDER BY
                    CASE WHEN seeding_state = 'green' THEN 1 ELSE 0 END,
                    seeding_next_attempt_at,
                    release_id,
                    sd_hash
                """,
                (
                    JobState.AWAITING_INDEX,
                    SeedingState.PENDING,
                    SeedingState.RETRYING,
                    SeedingState.BLOCKED,
                    SeedingState.GREEN,
                ),
            ).fetchall()
        return [
            SeedingCandidate(
                release=Release.from_api(json.loads(row["release_json"])),
                job=_job_from_row(row),
            )
            for row in rows
        ]

    def seeding_identity_candidates(
        self,
        info_hash: str,
        sha384: str,
    ) -> list[SeedingCandidate]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state=? AND info_hash=? AND sha384=?
                ORDER BY release_id, sd_hash
                """,
                (JobState.AWAITING_INDEX, info_hash, sha384),
            ).fetchall()
        return [
            SeedingCandidate(
                release=Release.from_api(json.loads(row["release_json"])),
                job=_job_from_row(row),
            )
            for row in rows
        ]

    def seeding_ready(self, job: Job) -> bool:
        return job.seeding_next_attempt_at <= self.clock()

    def start_seeding(self, release_id: str, sd_hash: str) -> Job | None:
        now = self.clock()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET
                    seeding_state=?,
                    seeding_attempts=seeding_attempts + 1,
                    seeding_next_attempt_at=0,
                    seeding_error_code=NULL,
                    seeding_error=NULL,
                    seeding_updated_at=?
                WHERE
                    release_id=? AND sd_hash=? AND state=? AND
                    seeding_state IN (?, ?, ?, ?) AND
                    seeding_next_attempt_at <= ?
                """,
                (
                    SeedingState.INJECTING,
                    now,
                    release_id,
                    sd_hash,
                    JobState.AWAITING_INDEX,
                    SeedingState.PENDING,
                    SeedingState.RETRYING,
                    SeedingState.BLOCKED,
                    SeedingState.GREEN,
                    now,
                ),
            )
        if cursor.rowcount != 1:
            return None
        return self.get(release_id, sd_hash)

    def retry_seeding(
        self,
        release_id: str,
        sd_hash: str,
        *,
        code: str,
        error: str,
        retry_backoff: float,
    ) -> Job:
        job = self.get(release_id, sd_hash)
        delay = retry_backoff * (2 ** min(max(job.seeding_attempts - 1, 0), 10))
        self._seeding_update(
            release_id,
            sd_hash,
            seeding_state=SeedingState.RETRYING,
            seeding_next_attempt_at=self.clock() + delay,
            seeding_error_code=code,
            seeding_error=error,
        )
        return self.get(release_id, sd_hash)

    def block_seeding(
        self,
        release_id: str,
        sd_hash: str,
        *,
        code: str,
        error: str,
        retry_after: float,
    ) -> Job:
        self._seeding_update(
            release_id,
            sd_hash,
            seeding_state=SeedingState.BLOCKED,
            seeding_next_attempt_at=self.clock() + retry_after,
            seeding_error_code=code,
            seeding_error=error,
        )
        return self.get(release_id, sd_hash)

    def mark_seed_green(
        self,
        release_id: str,
        sd_hash: str,
        *,
        client_version: str,
        observed_state: str,
        content_path: str,
        dht_nodes: int,
        working_trackers: int,
        recheck_interval: float,
    ) -> Job:
        now = self.clock()
        self._seeding_update(
            release_id,
            sd_hash,
            seeding_state=SeedingState.GREEN,
            seeding_attempts=0,
            seeding_next_attempt_at=now + recheck_interval,
            seeding_error_code=None,
            seeding_error=None,
            seeding_client="qbittorrent",
            seeding_client_version=client_version,
            seeding_observed_state=observed_state,
            seeding_content_path=content_path,
            seeding_dht_nodes=dht_nodes,
            seeding_working_trackers=working_trackers,
            seeding_checked_at=now,
        )
        return self.get(release_id, sd_hash)

    def next_seeding_delay(self) -> float | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT MIN(seeding_next_attempt_at) AS deadline
                FROM jobs
                WHERE state=? AND seeding_state IN (?, ?, ?, ?)
                """,
                (
                    JobState.AWAITING_INDEX,
                    SeedingState.PENDING,
                    SeedingState.RETRYING,
                    SeedingState.BLOCKED,
                    SeedingState.GREEN,
                ),
            ).fetchone()
        deadline = row["deadline"]
        if deadline is None:
            return None
        return max(float(deadline) - self.clock(), 0)

    def recover_interrupted_publications(self) -> int:
        now = self.clock()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET
                    publication_state=?,
                    publication_next_attempt_at=0,
                    publication_error_code='interrupted',
                    publication_error='Mirror stopped during publication',
                    publication_updated_at=?
                WHERE publication_state=?
                """,
                (
                    PublicationState.RETRYING,
                    now,
                    PublicationState.PUBLISHING,
                ),
            )
        return cursor.rowcount

    def publication_candidates(self) -> list[PublicationCandidate]:
        now = self.clock()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE
                    state=? AND
                    seeding_state=? AND
                    seeding_next_attempt_at > ? AND
                    publication_state IN (?, ?) AND
                    sha384 IS NOT NULL AND
                    torrent_path IS NOT NULL
                ORDER BY sha384, release_id, sd_hash
                """,
                (
                    JobState.AWAITING_INDEX,
                    SeedingState.GREEN,
                    now,
                    PublicationState.PENDING,
                    PublicationState.RETRYING,
                ),
            ).fetchall()
        candidates = []
        for row in rows:
            raw = json.loads(row["release_json"])
            candidates.append(
                PublicationCandidate(
                    release=Release.from_api(raw),
                    job=_job_from_row(row),
                )
            )
        return candidates

    def publication_ready(self, job: Job) -> bool:
        return job.publication_next_attempt_at <= self.clock()

    def start_publication(self, release_id: str, sd_hash: str) -> Job | None:
        now = self.clock()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET
                    publication_state=?,
                    publication_attempts=publication_attempts + 1,
                    publication_next_attempt_at=0,
                    publication_error_code=NULL,
                    publication_error=NULL,
                    publication_updated_at=?
                WHERE
                    release_id=? AND sd_hash=? AND state=? AND
                    seeding_state=? AND seeding_next_attempt_at > ? AND
                    publication_state IN (?, ?) AND
                    publication_next_attempt_at <= ?
                """,
                (
                    PublicationState.PUBLISHING,
                    now,
                    release_id,
                    sd_hash,
                    JobState.AWAITING_INDEX,
                    SeedingState.GREEN,
                    now,
                    PublicationState.PENDING,
                    PublicationState.RETRYING,
                    now,
                ),
            )
        if cursor.rowcount != 1:
            return None
        return self.get(release_id, sd_hash)

    def retry_publication(
        self,
        release_id: str,
        sd_hash: str,
        *,
        code: str,
        error: str,
        retry_backoff: float,
        retry_after: float | None = None,
    ) -> Job:
        job = self.get(release_id, sd_hash)
        exponential = retry_backoff * (
            2 ** min(max(job.publication_attempts - 1, 0), 10)
        )
        delay = max(exponential, retry_after or 0)
        self._publication_update(
            ((release_id, sd_hash),),
            publication_state=PublicationState.RETRYING,
            publication_next_attempt_at=self.clock() + delay,
            publication_error_code=code,
            publication_error=error,
        )
        return self.get(release_id, sd_hash)

    def finish_publication(
        self,
        jobs: Iterable[tuple[str, str]],
        *,
        state: PublicationState,
        outcome: str | None,
        canonical: bool | None,
        canonical_sha384: str | None,
        canonical_btih: str | None,
        canonical_torrent_url: str | None,
        canonical_magnet_uri: str | None,
        winning_release_id: str | None,
        error_code: str | None = None,
        error: str | None = None,
    ) -> None:
        if state not in {
            PublicationState.PUBLISHED,
            PublicationState.DUPLICATE,
            PublicationState.REJECTED,
            PublicationState.CONFLICT,
        }:
            raise ValueError(f"invalid terminal publication state: {state}")
        self._publication_update(
            jobs,
            publication_state=state,
            publication_next_attempt_at=0,
            publication_outcome=outcome,
            publication_canonical=(None if canonical is None else int(canonical)),
            canonical_sha384=canonical_sha384,
            canonical_btih=canonical_btih,
            canonical_torrent_url=canonical_torrent_url,
            canonical_magnet_uri=canonical_magnet_uri,
            winning_release_id=winning_release_id,
            publication_error_code=error_code,
            publication_error=error,
        )

    def next_publication_delay(self) -> float | None:
        now = self.clock()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT MIN(publication_next_attempt_at) AS deadline
                FROM jobs
                WHERE
                    state=? AND
                    seeding_state=? AND
                    seeding_next_attempt_at > ? AND
                    publication_state IN (?, ?)
                """,
                (
                    JobState.AWAITING_INDEX,
                    SeedingState.GREEN,
                    now,
                    PublicationState.PENDING,
                    PublicationState.RETRYING,
                ),
            ).fetchone()
        deadline = row["deadline"]
        if deadline is None:
            return None
        return max(float(deadline) - self.clock(), 0)

    def search_archive(
        self,
        query: str = "",
        *,
        platform: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ArchiveEntry], int]:
        if limit < 1 or limit > 200:
            raise ValueError("archive search limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("archive search offset cannot be negative")

        clauses = ["state=?"]
        parameters: list[object] = [JobState.AWAITING_INDEX]
        if platform:
            clauses.append("platform=?")
            parameters.append(platform.lower())
        for term in query.split():
            pattern = _like_pattern(term)
            clauses.append(
                """
                (
                    release_name LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    channel_handle LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    release_slug LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    release_id LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    sd_hash LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    info_hash LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    platform LIKE ? ESCAPE '\\' COLLATE NOCASE
                )
                """
            )
            parameters.extend(
                (pattern, pattern, pattern, pattern, pattern, pattern, pattern)
            )
        where = " AND ".join(clauses)

        with closing(self._connect()) as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {where}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT
                    release_id,
                    sd_hash,
                    release_name,
                    channel_handle,
                    release_slug,
                    release_json,
                    platform,
                    payload_size,
                    file_path,
                    magnet_uri,
                    seeding_state,
                    seeding_observed_state,
                    seeding_dht_nodes,
                    seeding_working_trackers,
                    seeding_error_code,
                    publication_state,
                    canonical_magnet_uri,
                    canonical_torrent_url
                FROM jobs
                WHERE {where}
                ORDER BY
                    channel_handle COLLATE NOCASE,
                    release_name COLLATE NOCASE,
                    release_id,
                    sd_hash
                LIMIT ? OFFSET ?
                """,
                [*parameters, limit, offset],
            ).fetchall()
        return [_archive_entry_from_row(row) for row in rows], total

    def retry_job(self, release_id: str, sd_hash: str) -> bool:
        now = self.clock()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state=?, attempts=0, next_attempt_at=0, last_error=NULL, exclusion_reason=NULL, updated_at=?
                WHERE release_id=? AND sd_hash=? AND state IN (?, ?)
                """,
                (
                    JobState.PENDING,
                    now,
                    release_id,
                    sd_hash,
                    JobState.FAILED,
                    JobState.EXCLUDED,
                ),
            )
            return cursor.rowcount > 0

    def exclude_job(
        self,
        release_id: str,
        sd_hash: str,
        reason: str = "Non-model software",
    ) -> bool:
        now = self.clock()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state=?, exclusion_reason=?, last_error=NULL, updated_at=?
                WHERE release_id=? AND sd_hash=?
                """,
                (
                    JobState.EXCLUDED,
                    reason.strip() or "Non-model software",
                    now,
                    release_id,
                    sd_hash,
                ),
            )
            return cursor.rowcount > 0

    def get_category_entries(
        self,
        section: str,
        category: str = "all",
        *,
        platform: str | None = None,
        query: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        if limit < 1 or limit > 200:
            raise ValueError("category entries limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("category entries offset cannot be negative")

        section = section.strip().lower()
        category = category.strip().lower()
        clauses: list[str] = []
        parameters: list[object] = []

        if section == "pipeline":
            if category == "pending":
                clauses.append("state=?")
                parameters.append(JobState.PENDING)
            elif category == "acquiring":
                clauses.append("state=?")
                parameters.append(JobState.ACQUIRING)
            elif category == "verified":
                clauses.append("state=?")
                parameters.append(JobState.VERIFIED)
            elif category in ("awaiting_index", "staged"):
                clauses.append("state=?")
                parameters.append(JobState.AWAITING_INDEX)
            elif category == "excluded":
                clauses.append("state=?")
                parameters.append(JobState.EXCLUDED)
            elif category == "failed":
                clauses.append("state=?")
                parameters.append(JobState.FAILED)
            elif category in ("inflight", "in-flight"):
                clauses.append("state IN (?, ?)")
                parameters.extend([JobState.PENDING, JobState.ACQUIRING])
        elif section == "publication":
            if category == "pending":
                clauses.append("publication_state=?")
                parameters.append(PublicationState.PENDING)
            elif category == "publishing":
                clauses.append("publication_state=?")
                parameters.append(PublicationState.PUBLISHING)
            elif category == "retrying":
                clauses.append("publication_state=?")
                parameters.append(PublicationState.RETRYING)
            elif category in ("published", "accepted"):
                clauses.append("publication_state=?")
                parameters.append(PublicationState.PUBLISHED)
            elif category in ("duplicate", "duplicates"):
                clauses.append("publication_state=?")
                parameters.append(PublicationState.DUPLICATE)
            elif category == "rejected":
                clauses.append("publication_state=?")
                parameters.append(PublicationState.REJECTED)
            elif category in ("conflict", "conflicts"):
                clauses.append("publication_state=?")
                parameters.append(PublicationState.CONFLICT)
        elif section == "seeding":
            if category == "pending":
                clauses.append("seeding_state=?")
                parameters.append(SeedingState.PENDING)
            elif category == "injecting":
                clauses.append("seeding_state=?")
                parameters.append(SeedingState.INJECTING)
            elif category == "retrying":
                clauses.append("seeding_state=?")
                parameters.append(SeedingState.RETRYING)
            elif category == "green":
                clauses.append("seeding_state=?")
                parameters.append(SeedingState.GREEN)
            elif category == "blocked":
                clauses.append("seeding_state=?")
                parameters.append(SeedingState.BLOCKED)
        elif section == "source":
            if category in ("awaiting_index", "staged"):
                clauses.append("state=?")
                parameters.append(JobState.AWAITING_INDEX)
            elif category in ("seeding_green", "green"):
                clauses.append("state=?")
                parameters.append(JobState.AWAITING_INDEX)
                clauses.append("seeding_state=?")
                parameters.append(SeedingState.GREEN)
            elif category in ("published",):
                clauses.append("publication_state IN (?, ?)")
                parameters.extend(
                    [PublicationState.PUBLISHED, PublicationState.DUPLICATE]
                )
            elif category in ("inflight", "in-flight"):
                clauses.append("state IN (?, ?)")
                parameters.extend([JobState.PENDING, JobState.ACQUIRING])
            elif category == "failed":
                clauses.append("state=?")
                parameters.append(JobState.FAILED)
            elif category == "excluded":
                clauses.append("state=?")
                parameters.append(JobState.EXCLUDED)

        if platform:
            clauses.append("platform=?")
            parameters.append(platform.lower())

        for term in query.split():
            pattern = _like_pattern(term)
            clauses.append(
                """
                (
                    release_name LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    channel_handle LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    release_slug LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    release_id LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    last_error LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    exclusion_reason LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    publication_error LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    seeding_error LIKE ? ESCAPE '\\' COLLATE NOCASE
                )
                """
            )
            parameters.extend([pattern] * 8)

        where = " AND ".join(clauses) if clauses else "1=1"

        if category in ("failed", "rejected", "conflict", "blocked"):
            order_by = "ORDER BY updated_at DESC, release_id ASC"
        else:
            order_by = "ORDER BY updated_at DESC, channel_handle COLLATE NOCASE ASC, release_name COLLATE NOCASE ASC"

        with closing(self._connect()) as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {where}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT
                    release_id,
                    sd_hash,
                    release_json,
                    release_name,
                    channel_handle,
                    release_slug,
                    platform,
                    payload_size,
                    state,
                    attempts,
                    next_attempt_at,
                    file_path,
                    torrent_path,
                    info_hash,
                    magnet_uri,
                    last_error,
                    exclusion_reason,
                    seeding_state,
                    seeding_attempts,
                    seeding_error_code,
                    seeding_error,
                    seeding_observed_state,
                    seeding_dht_nodes,
                    seeding_working_trackers,
                    publication_state,
                    publication_attempts,
                    publication_outcome,
                    publication_error_code,
                    publication_error,
                    winning_release_id,
                    updated_at
                FROM jobs
                WHERE {where}
                {order_by}
                LIMIT ? OFFSET ?
                """,
                [*parameters, limit, offset],
            ).fetchall()

        entries = []
        for row in rows:
            entries.append(
                {
                    "release_id": row["release_id"],
                    "sd_hash": row["sd_hash"],
                    "release_name": row["release_name"],
                    "channel_handle": row["channel_handle"],
                    "release_slug": row["release_slug"],
                    "platform": row["platform"] or "lbry",
                    "payload_size": row["payload_size"],
                    "source_url": _source_url_from_json(row["release_json"]),
                    "state": row["state"],
                    "attempts": row["attempts"],
                    "next_attempt_at": row["next_attempt_at"],
                    "has_payload": bool(row["file_path"]),
                    "has_torrent": bool(row["torrent_path"]),
                    "info_hash": row["info_hash"],
                    "magnet_uri": row["magnet_uri"],
                    "last_error": row["last_error"],
                    "exclusion_reason": row["exclusion_reason"],
                    "seeding_state": row["seeding_state"],
                    "seeding_attempts": row["seeding_attempts"],
                    "seeding_error_code": row["seeding_error_code"],
                    "seeding_error": row["seeding_error"],
                    "seeding_observed_state": row["seeding_observed_state"],
                    "seeding_dht_nodes": row["seeding_dht_nodes"],
                    "seeding_working_trackers": row["seeding_working_trackers"],
                    "publication_state": row["publication_state"],
                    "publication_attempts": row["publication_attempts"],
                    "publication_outcome": row["publication_outcome"],
                    "publication_error_code": row["publication_error_code"],
                    "publication_error": row["publication_error"],
                    "winning_release_id": row["winning_release_id"],
                    "updated_at": row["updated_at"],
                }
            )
        return entries, total

    def load_tracker_policy_cache(
        self,
        endpoint: str,
    ) -> TrackerPolicyCache | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT endpoint, etag, document, updated_at
                FROM tracker_policy_cache
                WHERE singleton=1 AND endpoint=?
                """,
                (endpoint,),
            ).fetchone()
        if row is None:
            return None
        document = row["document"]
        if isinstance(document, str):
            document = document.encode()
        return TrackerPolicyCache(
            endpoint=row["endpoint"],
            etag=row["etag"],
            document=bytes(document),
            updated_at=float(row["updated_at"]),
        )

    def save_tracker_policy_cache(
        self,
        endpoint: str,
        etag: str,
        document: bytes,
    ) -> TrackerPolicyCache:
        now = self.clock()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO tracker_policy_cache (
                    singleton, endpoint, etag, document, updated_at
                ) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    endpoint=excluded.endpoint,
                    etag=excluded.etag,
                    document=excluded.document,
                    updated_at=excluded.updated_at
                """,
                (endpoint, etag, document, now),
            )
        cached = self.load_tracker_policy_cache(endpoint)
        if cached is None:  # pragma: no cover - transaction invariant
            raise RuntimeError("tracker policy cache write did not persist")
        return cached

    @staticmethod
    def _backfill_archive_fields(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT release_id, sd_hash, release_json
            FROM jobs
            WHERE release_name='' OR channel_handle='' OR release_slug=''
            """
        ).fetchall()
        updates = []
        for row in rows:
            name, channel_handle, slug = _archive_fields_from_json(
                row["release_id"], row["release_json"]
            )
            updates.append(
                (name, channel_handle, slug, row["release_id"], row["sd_hash"])
            )
        connection.executemany(
            """
            UPDATE jobs SET
                release_name=?, channel_handle=?, release_slug=?
            WHERE release_id=? AND sd_hash=?
            """,
            updates,
        )

    @staticmethod
    def _backfill_platform_and_size(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT release_id, sd_hash, release_json
            FROM jobs
            WHERE platform=''
            """
        ).fetchall()
        updates = []
        for row in rows:
            platform = _platform_from_json(row["release_json"])
            size = _release_size_from_json(row["release_json"])
            updates.append((platform, size, row["release_id"], row["sd_hash"]))
        if updates:
            connection.executemany(
                """
                UPDATE jobs SET
                    platform=?, payload_size=?
                WHERE release_id=? AND sd_hash=?
                """,
                updates,
            )

    def _update(self, release: Release, **fields: object) -> None:
        if not fields:
            return
        fields["updated_at"] = self.clock()
        assignments = ", ".join(f"{field}=?" for field in fields)
        values = list(fields.values()) + [release.id, release.sd_hash]
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE release_id=? AND sd_hash=?",
                values,
            )

    def _publication_update(
        self,
        jobs: Iterable[tuple[str, str]],
        **fields: object,
    ) -> None:
        keys = tuple(jobs)
        if not keys or not fields:
            return
        fields["publication_updated_at"] = self.clock()
        assignments = ", ".join(f"{field}=?" for field in fields)
        parameters = [
            [*fields.values(), release_id, sd_hash] for release_id, sd_hash in keys
        ]
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                f"UPDATE jobs SET {assignments} WHERE release_id=? AND sd_hash=?",
                parameters,
            )

    def _seeding_update(
        self,
        release_id: str,
        sd_hash: str,
        **fields: object,
    ) -> None:
        if not fields:
            return
        fields["seeding_updated_at"] = self.clock()
        assignments = ", ".join(f"{field}=?" for field in fields)
        values = [*fields.values(), release_id, sd_hash]
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE release_id=? AND sd_hash=?",
                values,
            )


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        release_id=row["release_id"],
        sd_hash=row["sd_hash"],
        state=JobState(row["state"]),
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        file_path=Path(row["file_path"]) if row["file_path"] else None,
        sha384=row["sha384"],
        sha256=row["sha256"],
        torrent_path=Path(row["torrent_path"]) if row["torrent_path"] else None,
        info_hash=row["info_hash"],
        magnet_uri=row["magnet_uri"],
        last_error=row["last_error"],
        exclusion_reason=row["exclusion_reason"],
        seeding_state=SeedingState(row["seeding_state"]),
        seeding_attempts=row["seeding_attempts"],
        seeding_next_attempt_at=row["seeding_next_attempt_at"],
        seeding_error_code=row["seeding_error_code"],
        seeding_error=row["seeding_error"],
        seeding_client=row["seeding_client"],
        seeding_client_version=row["seeding_client_version"],
        seeding_observed_state=row["seeding_observed_state"],
        seeding_content_path=row["seeding_content_path"],
        seeding_dht_nodes=row["seeding_dht_nodes"],
        seeding_working_trackers=row["seeding_working_trackers"],
        seeding_checked_at=row["seeding_checked_at"],
        seeding_updated_at=row["seeding_updated_at"],
        publication_state=PublicationState(row["publication_state"]),
        publication_attempts=row["publication_attempts"],
        publication_next_attempt_at=row["publication_next_attempt_at"],
        publication_outcome=row["publication_outcome"],
        publication_canonical=(
            None
            if row["publication_canonical"] is None
            else bool(row["publication_canonical"])
        ),
        canonical_sha384=row["canonical_sha384"],
        canonical_btih=row["canonical_btih"],
        canonical_torrent_url=row["canonical_torrent_url"],
        canonical_magnet_uri=row["canonical_magnet_uri"],
        winning_release_id=row["winning_release_id"],
        publication_error_code=row["publication_error_code"],
        publication_error=row["publication_error"],
        publication_updated_at=row["publication_updated_at"],
        platform=(
            row["platform"] if "platform" in row.keys() and row["platform"] else "lbry"
        ),
        payload_size=row["payload_size"] if "payload_size" in row.keys() else None,
    )


def _release_slug(release: Release) -> str:
    origin = release.raw.get("origin")
    if isinstance(origin, Mapping):
        slug = origin.get("slug")
        if isinstance(slug, str) and slug.strip():
            return slug
    return release.name


def _archive_fields_from_json(
    release_id: str, release_json: str
) -> tuple[str, str, str]:
    try:
        value = json.loads(release_json)
    except (json.JSONDecodeError, TypeError):
        return release_id, "Unknown channel", release_id
    if not isinstance(value, Mapping):
        return release_id, "Unknown channel", release_id

    raw_name = value.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name.strip() else release_id
    channel = value.get("channel")
    raw_handle = channel.get("handle") if isinstance(channel, Mapping) else None
    channel_handle = (
        raw_handle
        if isinstance(raw_handle, str) and raw_handle.strip()
        else "Unknown channel"
    )
    origin = value.get("origin")
    raw_slug = origin.get("slug") if isinstance(origin, Mapping) else None
    slug = raw_slug if isinstance(raw_slug, str) and raw_slug.strip() else name
    return name, channel_handle, slug


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _archive_entry_from_row(row: sqlite3.Row) -> ArchiveEntry:
    raw_path = row["file_path"]
    path = Path(raw_path) if raw_path else None
    size = (
        row["payload_size"]
        if "payload_size" in row.keys() and row["payload_size"] is not None
        else _release_size_from_json(row["release_json"])
    )
    platform = (
        row["platform"]
        if "platform" in row.keys() and row["platform"]
        else _platform_from_json(row["release_json"])
    )
    return ArchiveEntry(
        release_id=row["release_id"],
        sd_hash=row["sd_hash"],
        name=row["release_name"],
        channel_handle=row["channel_handle"],
        slug=row["release_slug"],
        file_name=path.name if path is not None else "payload",
        size=size,
        magnet_uri=row["magnet_uri"],
        seeding_state=SeedingState(row["seeding_state"]),
        seeding_observed_state=row["seeding_observed_state"],
        seeding_dht_nodes=row["seeding_dht_nodes"],
        seeding_working_trackers=row["seeding_working_trackers"],
        seeding_error_code=row["seeding_error_code"],
        publication_state=PublicationState(row["publication_state"]),
        canonical_magnet_uri=row["canonical_magnet_uri"],
        canonical_torrent_url=row["canonical_torrent_url"],
        platform=platform,
    )


def _release_size_from_json(release_json: str) -> int | None:
    try:
        value = json.loads(release_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, Mapping):
        return None
    origin = value.get("origin")
    raw_size = origin.get("size") if isinstance(origin, Mapping) else None
    if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size >= 0:
        return raw_size
    return None


def _platform_from_json(release_json: str) -> str:
    try:
        value = json.loads(release_json)
    except (json.JSONDecodeError, TypeError):
        return "lbry"
    if not isinstance(value, Mapping):
        return "lbry"
    origin = value.get("origin")
    if isinstance(origin, Mapping):
        plat = origin.get("platform")
        if isinstance(plat, str) and plat.strip().lower() in SUPPORTED_PLATFORMS:
            return plat.strip().lower()
    return "lbry"


def _source_url_from_json(release_json: str) -> str | None:
    try:
        value = json.loads(release_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, Mapping):
        return None
    origin = value.get("origin")
    if not isinstance(origin, Mapping):
        return None
    links = origin.get("links")
    if isinstance(links, list):
        for scheme in ("https", "http", "lbry"):
            for link in links:
                if isinstance(link, Mapping):
                    u = link.get("url")
                    if isinstance(u, str) and u.strip():
                        if u.strip().lower().startswith(f"{scheme}:"):
                            return u.strip()
    url_field = origin.get("url")
    if isinstance(url_field, str) and url_field.strip():
        return url_field.strip()
    return None
