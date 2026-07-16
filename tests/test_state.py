from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from guncadmirror.models import JobState, TorrentArtifact
from guncadmirror.state import JobStore

from .helpers import make_release


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.now = 100.0
        self.store = JobStore(
            Path(self.temporary.name) / "state" / "mirror.sqlite3",
            clock=lambda: self.now,
        )
        self.release = make_release()

    def test_job_lifecycle_is_durable_and_registration_is_idempotent(self) -> None:
        job = self.store.register(self.release)
        self.assertEqual(job.state, JobState.PENDING)
        self.assertEqual(job.attempts, 0)
        self.assertTrue(self.store.ready_for_attempt(job))

        job = self.store.start_attempt(self.release)
        self.assertEqual(job.state, JobState.ACQUIRING)
        self.assertEqual(job.attempts, 1)

        payload = Path(self.temporary.name) / "payload.zip"
        payload.write_bytes(b"payload")
        job = self.store.mark_verified(
            self.release,
            file_path=payload,
            sha384="c" * 96,
            sha256="d" * 64,
        )
        self.assertEqual(job.state, JobState.VERIFIED)
        self.assertEqual(job.file_path, payload)
        self.assertEqual(job.sha384, "c" * 96)
        self.assertEqual(job.sha256, "d" * 64)

        torrent_path = Path(self.temporary.name) / "payload.torrent"
        torrent_path.write_bytes(b"torrent")
        torrent = TorrentArtifact(
            file_path=payload,
            torrent_path=torrent_path,
            piece_length=1024**2,
            piece_count=1,
            info_hash="e" * 40,
            torrent_sha256="f" * 64,
            magnet_uri="magnet:?xt=urn:btih:" + "e" * 40,
            trackers=(),
        )
        job = self.store.mark_awaiting_index(self.release, torrent)
        self.assertEqual(job.state, JobState.AWAITING_INDEX)
        self.assertFalse(self.store.ready_for_attempt(job))
        self.assertEqual(job.torrent_path, torrent_path)
        self.assertEqual(job.info_hash, "e" * 40)

        # Refreshing Index metadata must not erase completed work.
        refreshed = self.store.register(self.release)
        self.assertEqual(refreshed.state, JobState.AWAITING_INDEX)
        self.assertEqual(refreshed.attempts, 1)
        self.assertEqual(self.store.counts(), {"awaiting_index": 1})

        reopened = JobStore(self.store.path, clock=lambda: self.now)
        self.assertEqual(reopened.get(self.release.id, self.release.sd_hash), refreshed)

    def test_failure_backoff_grows_with_attempt_count_and_clears_on_retry(self) -> None:
        self.store.register(self.release)
        self.store.start_attempt(self.release)
        job = self.store.mark_failed(
            self.release, ValueError("bad bytes"), retry_backoff=5
        )
        self.assertEqual(job.state, JobState.FAILED)
        self.assertEqual(job.next_attempt_at, 105)
        self.assertEqual(job.last_error, "ValueError: bad bytes")
        self.assertFalse(self.store.ready_for_attempt(job))

        self.now = 105
        self.assertTrue(self.store.ready_for_attempt(job))
        job = self.store.start_attempt(self.release)
        self.assertEqual(job.attempts, 2)
        self.assertIsNone(job.last_error)
        job = self.store.mark_failed(
            self.release, RuntimeError("again"), retry_backoff=5
        )
        self.assertEqual(job.next_attempt_at, 115)

    def test_policy_exclusion_is_durable_and_clears_when_retried(self) -> None:
        self.store.register(self.release)
        job = self.store.mark_excluded(self.release, "payload exceeds limit")

        self.assertEqual(job.state, JobState.EXCLUDED)
        self.assertEqual(job.exclusion_reason, "payload exceeds limit")
        self.assertTrue(self.store.ready_for_attempt(job))

        job = self.store.start_attempt(self.release)
        self.assertEqual(job.state, JobState.ACQUIRING)
        self.assertIsNone(job.exclusion_reason)

    def test_existing_ledger_is_migrated_for_exclusion_reasons(self) -> None:
        path = Path(self.temporary.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE jobs (
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
                )
                """
            )

        migrated = JobStore(path)
        with closing(sqlite3.connect(path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        self.assertIn("exclusion_reason", columns)
        self.assertEqual(migrated.counts(), {})

    def test_missing_job_raises_key_error_and_empty_counts_are_valid(self) -> None:
        self.assertEqual(self.store.counts(), {})
        with self.assertRaises(KeyError):
            self.store.get("x", "y")


if __name__ == "__main__":
    unittest.main()
