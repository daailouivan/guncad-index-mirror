from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Callable

from .models import JobState, Release, TorrentArtifact

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    release_id TEXT NOT NULL,
    sd_hash TEXT NOT NULL,
    release_json TEXT NOT NULL,
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


class JobStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time):
        self.path = path
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def register(self, release: Release) -> Job:
        now = self.clock()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    release_id, sd_hash, release_json, state, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(release_id, sd_hash) DO UPDATE SET
                    release_json=excluded.release_json,
                    updated_at=excluded.updated_at
                """,
                (
                    release.id,
                    release.sd_hash,
                    release.to_json(),
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
                    last_error=NULL, updated_at=?
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
    )
