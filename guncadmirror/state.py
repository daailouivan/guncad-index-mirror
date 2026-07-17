from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from time import time

from .models import JobState, Release, TorrentArtifact

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    release_id TEXT NOT NULL,
    sd_hash TEXT NOT NULL,
    release_json TEXT NOT NULL,
    release_name TEXT NOT NULL DEFAULT '',
    channel_handle TEXT NOT NULL DEFAULT '',
    release_slug TEXT NOT NULL DEFAULT '',
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
    updated_at REAL NOT NULL,
    PRIMARY KEY (release_id, sd_hash)
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


class JobStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time):
        self.path = path
        self.clock = clock
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
            }
            for column, definition in archive_columns.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE jobs ADD COLUMN {column} {definition}"
                    )
            self._backfill_archive_fields(connection)
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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def register(self, release: Release) -> Job:
        now = self.clock()
        release_slug = _release_slug(release)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    release_id, sd_hash, release_json, release_name,
                    channel_handle, release_slug, state, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(release_id, sd_hash) DO UPDATE SET
                    release_json=excluded.release_json,
                    release_name=excluded.release_name,
                    channel_handle=excluded.channel_handle,
                    release_slug=excluded.release_slug,
                    updated_at=excluded.updated_at
                """,
                (
                    release.id,
                    release.sd_hash,
                    release.to_json(),
                    release.name,
                    release.channel_handle,
                    release_slug,
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
        self._update(
            release,
            state=JobState.VERIFIED,
            file_path=str(file_path),
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
            last_error=None,
            exclusion_reason=None,
        )
        return self.get(release.id, release.sd_hash)

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

    def search_archive(
        self,
        query: str = "",
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ArchiveEntry], int]:
        if limit < 1 or limit > 200:
            raise ValueError("archive search limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("archive search offset cannot be negative")

        clauses = ["state=?"]
        parameters: list[object] = [JobState.AWAITING_INDEX]
        for term in query.split():
            pattern = _like_pattern(term)
            clauses.append(
                """
                (
                    release_name LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    channel_handle LIKE ? ESCAPE '\\' COLLATE NOCASE OR
                    release_slug LIKE ? ESCAPE '\\' COLLATE NOCASE
                )
                """
            )
            parameters.extend((pattern, pattern, pattern))
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
                    file_path,
                    magnet_uri
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
    return ArchiveEntry(
        release_id=row["release_id"],
        sd_hash=row["sd_hash"],
        name=row["release_name"],
        channel_handle=row["channel_handle"],
        slug=row["release_slug"],
        file_name=path.name if path is not None else "payload",
        size=_release_size_from_json(row["release_json"]),
        magnet_uri=row["magnet_uri"],
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
