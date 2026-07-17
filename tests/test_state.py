from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from guncadmirror.models import JobState, Release, TorrentArtifact
from guncadmirror.state import JobStore

from .helpers import make_release, release_payload


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

    def _complete(self, release: Release, *, content: bytes = b"payload") -> None:
        payload = Path(self.temporary.name) / "payloads" / f"{release.id}.zip"
        payload.parent.mkdir(exist_ok=True)
        payload.write_bytes(content)
        torrent_path = Path(self.temporary.name) / "outbox" / f"{release.id}.torrent"
        torrent_path.parent.mkdir(exist_ok=True)
        torrent_path.write_bytes(b"torrent")
        self.store.register(release)
        self.store.start_attempt(release)
        self.store.mark_verified(
            release,
            file_path=payload,
            sha384="c" * 96,
            sha256="d" * 64,
        )
        self.store.mark_awaiting_index(
            release,
            TorrentArtifact(
                file_path=payload,
                torrent_path=torrent_path,
                piece_length=1024**2,
                piece_count=1,
                info_hash="e" * 40,
                torrent_sha256="f" * 64,
                magnet_uri="magnet:?xt=urn:btih:" + "e" * 40,
                trackers=(),
            ),
        )

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
        legacy_payload = release_payload(name="Legacy Searchable Release")
        legacy_payload["origin"]["slug"] = "legacy-slug:l"
        legacy_release = Release.from_api(legacy_payload)
        payload_path = Path(self.temporary.name) / "legacy.zip"
        payload_path.write_bytes(b"legacy")
        torrent_path = Path(self.temporary.name) / "legacy.torrent"
        torrent_path.write_bytes(b"torrent")
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
            connection.execute(
                """
                INSERT INTO jobs (
                    release_id, sd_hash, release_json, state, file_path,
                    torrent_path, magnet_uri, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    legacy_release.id,
                    legacy_release.sd_hash,
                    legacy_release.to_json(),
                    JobState.AWAITING_INDEX,
                    str(payload_path),
                    str(torrent_path),
                    "magnet:?xt=urn:btih:" + "e" * 40,
                    1,
                ),
            )

        migrated = JobStore(path)
        with closing(sqlite3.connect(path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        self.assertIn("exclusion_reason", columns)
        self.assertIn("release_name", columns)
        self.assertIn("channel_handle", columns)
        self.assertIn("release_slug", columns)
        entries, total = migrated.search_archive("legacy-slug")
        self.assertEqual(total, 1)
        self.assertEqual(entries[0].name, "Legacy Searchable Release")
        self.assertEqual(entries[0].channel_handle, "@channel:c")
        self.assertEqual(entries[0].slug, "legacy-slug:l")

    def test_archive_search_is_filtered_paginated_and_literal(self) -> None:
        payloads = [
            release_payload(
                release_id="1" * 40,
                sd_hash="1" * 96,
                channel="@Maker:a",
                name="Alpha 100% Tool",
            ),
            release_payload(
                release_id="2" * 40,
                sd_hash="2" * 96,
                channel="@Maker:a",
                name="Beta Fixture",
            ),
            release_payload(
                release_id="3" * 40,
                sd_hash="3" * 96,
                channel="@Other:b",
                name="Unfinished",
            ),
        ]
        payloads[0]["origin"]["slug"] = "alpha-special:a"
        payloads[1]["origin"]["slug"] = "beta:b"
        releases = [Release.from_api(payload) for payload in payloads]
        self._complete(releases[0])
        self._complete(releases[1])
        self.store.register(releases[2])

        entries, total = self.store.search_archive("maker fixture")
        self.assertEqual(total, 1)
        self.assertEqual(entries[0].name, "Beta Fixture")
        self.assertEqual(entries[0].file_name, f"{'2' * 40}.zip")
        self.assertEqual(entries[0].size, len(b"payload"))

        entries, total = self.store.search_archive("alpha-special")
        self.assertEqual(total, 1)
        self.assertEqual(entries[0].name, "Alpha 100% Tool")

        entries, total = self.store.search_archive("%")
        self.assertEqual(total, 1)
        self.assertEqual(entries[0].name, "Alpha 100% Tool")

        first, total = self.store.search_archive(limit=1)
        second, _ = self.store.search_archive(limit=1, offset=1)
        self.assertEqual(total, 2)
        self.assertEqual(first[0].name, "Alpha 100% Tool")
        self.assertEqual(second[0].name, "Beta Fixture")

        with self.assertRaises(ValueError):
            self.store.search_archive(limit=0)
        with self.assertRaises(ValueError):
            self.store.search_archive(offset=-1)

    def test_missing_job_raises_key_error_and_empty_counts_are_valid(self) -> None:
        self.assertEqual(self.store.counts(), {})
        with self.assertRaises(KeyError):
            self.store.get("x", "y")


if __name__ == "__main__":
    unittest.main()
