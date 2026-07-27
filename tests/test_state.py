from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from guncadmirror.models import (
    JobState,
    PublicationState,
    Release,
    SeedingState,
    TorrentArtifact,
)
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

    def _complete(
        self,
        release: Release,
        *,
        content: bytes = b"payload",
        seeded: bool = True,
    ) -> None:
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
        if seeded:
            self.store.mark_seed_green(
                release.id,
                release.sd_hash,
                client_version="v5.2.3",
                observed_state="forcedUP",
                content_path=str(payload),
                dht_nodes=42,
                working_trackers=0,
                recheck_interval=300,
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
        self.assertEqual(job.publication_state, PublicationState.PENDING)
        self.assertEqual(job.seeding_state, SeedingState.PENDING)
        self.assertEqual(job.publication_attempts, 0)
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
        self.assertIn("publication_state", columns)
        self.assertIn("canonical_torrent_url", columns)
        self.assertIn("seeding_state", columns)
        self.assertIn("seeding_checked_at", columns)
        entries, total = migrated.search_archive("legacy-slug")
        self.assertEqual(total, 1)
        self.assertEqual(entries[0].name, "Legacy Searchable Release")
        self.assertEqual(entries[0].channel_handle, "@channel:c")
        self.assertEqual(entries[0].slug, "legacy-slug:l")
        self.assertEqual(entries[0].publication_state, PublicationState.PENDING)

    def test_seeding_lifecycle_gates_publication_and_recovers_interruptions(
        self,
    ) -> None:
        self._complete(self.release, seeded=False)
        job = self.store.get(self.release.id, self.release.sd_hash)
        self.assertEqual(job.seeding_state, SeedingState.PENDING)
        self.assertEqual(self.store.seeding_counts(), {"pending": 1})
        self.assertEqual(self.store.next_seeding_delay(), 0)
        self.assertEqual(self.store.publication_candidates(), [])
        self.assertIsNone(
            self.store.start_publication(self.release.id, self.release.sd_hash)
        )

        injecting = self.store.start_seeding(self.release.id, self.release.sd_hash)
        self.assertEqual(injecting.seeding_state, SeedingState.INJECTING)
        self.assertEqual(injecting.seeding_attempts, 1)
        retrying = self.store.retry_seeding(
            self.release.id,
            self.release.sd_hash,
            code="network_error",
            error="connection reset",
            retry_backoff=5,
        )
        self.assertEqual(retrying.seeding_state, SeedingState.RETRYING)
        self.assertEqual(retrying.seeding_next_attempt_at, 105)
        self.assertFalse(self.store.seeding_ready(retrying))

        self.now = 105
        self.store.start_seeding(self.release.id, self.release.sd_hash)
        blocked = self.store.block_seeding(
            self.release.id,
            self.release.sd_hash,
            code="content_path_conflict",
            error="wrong path",
            retry_after=30,
        )
        self.assertEqual(blocked.seeding_state, SeedingState.BLOCKED)
        self.assertEqual(blocked.seeding_next_attempt_at, 135)

        self.now = 135
        self.store.start_seeding(self.release.id, self.release.sd_hash)
        green = self.store.mark_seed_green(
            self.release.id,
            self.release.sd_hash,
            client_version="v5.2.3",
            observed_state="forcedUP",
            content_path="/downloads/payload.zip",
            dht_nodes=12,
            working_trackers=1,
            recheck_interval=60,
        )
        self.assertEqual(green.seeding_state, SeedingState.GREEN)
        self.assertEqual(green.seeding_attempts, 0)
        self.assertEqual(green.seeding_next_attempt_at, 195)
        self.assertEqual(green.seeding_checked_at, 135)
        self.assertEqual(len(self.store.publication_candidates()), 1)

        self.now = 196
        self.assertEqual(self.store.publication_candidates(), [])
        self.assertTrue(self.store.seeding_ready(green))
        self.store.start_seeding(self.release.id, self.release.sd_hash)
        self.assertEqual(self.store.recover_interrupted_seeding(), 1)
        recovered = self.store.get(self.release.id, self.release.sd_hash)
        self.assertEqual(recovered.seeding_state, SeedingState.RETRYING)
        self.assertEqual(recovered.seeding_error_code, "interrupted")
        self.assertEqual(self.store.recover_interrupted_seeding(), 0)

    def test_publication_lifecycle_is_independent_durable_and_retryable(self) -> None:
        self._complete(self.release)
        self.assertEqual(self.store.publication_counts(), {"pending": 1})
        candidates = self.store.publication_candidates()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].release, self.release)

        publishing = self.store.start_publication(self.release.id, self.release.sd_hash)
        self.assertIsNotNone(publishing)
        self.assertEqual(publishing.publication_state, PublicationState.PUBLISHING)
        self.assertEqual(publishing.publication_attempts, 1)
        self.assertIsNone(
            self.store.start_publication(self.release.id, self.release.sd_hash)
        )

        retrying = self.store.retry_publication(
            self.release.id,
            self.release.sd_hash,
            code="network_error",
            error="connection reset",
            retry_backoff=5,
            retry_after=7,
        )
        self.assertEqual(retrying.publication_state, PublicationState.RETRYING)
        self.assertEqual(retrying.publication_next_attempt_at, 107)
        self.assertEqual(retrying.publication_error_code, "network_error")
        self.assertEqual(self.store.next_publication_delay(), 7)
        self.assertFalse(
            self.store.publication_ready(self.store.publication_candidates()[0].job)
        )

        self.now = 107
        self.assertEqual(len(self.store.publication_candidates()), 1)
        self.assertTrue(
            self.store.publication_ready(self.store.publication_candidates()[0].job)
        )
        publishing = self.store.start_publication(self.release.id, self.release.sd_hash)
        self.assertEqual(publishing.publication_attempts, 2)
        self.store.finish_publication(
            ((self.release.id, self.release.sd_hash),),
            state=PublicationState.PUBLISHED,
            outcome="created",
            canonical=True,
            canonical_sha384="c" * 96,
            canonical_btih="e" * 40,
            canonical_torrent_url="https://index.example/torrents/e.torrent",
            canonical_magnet_uri="magnet:?xt=urn:btih:" + "e" * 40,
            winning_release_id=self.release.id,
        )
        published = self.store.get(self.release.id, self.release.sd_hash)
        self.assertEqual(published.state, JobState.AWAITING_INDEX)
        self.assertEqual(published.publication_state, PublicationState.PUBLISHED)
        self.assertEqual(published.publication_outcome, "created")
        self.assertTrue(published.publication_canonical)
        self.assertEqual(published.canonical_sha384, "c" * 96)
        self.assertEqual(published.winning_release_id, self.release.id)
        self.assertIsNone(self.store.next_publication_delay())

        with self.assertRaises(ValueError):
            self.store.finish_publication(
                ((self.release.id, self.release.sd_hash),),
                state=PublicationState.RETRYING,
                outcome=None,
                canonical=None,
                canonical_sha384=None,
                canonical_btih=None,
                canonical_torrent_url=None,
                canonical_magnet_uri=None,
                winning_release_id=None,
            )

    def test_publication_alias_completion_and_crash_recovery(self) -> None:
        alias_payload = release_payload(
            release_id="1" * 40,
            sd_hash=self.release.sd_hash,
            name="Alias",
        )
        alias = Release.from_api(alias_payload)
        self._complete(self.release)
        self._complete(alias)
        self.store.start_publication(self.release.id, self.release.sd_hash)

        self.assertEqual(self.store.recover_interrupted_publications(), 1)
        recovered = self.store.get(self.release.id, self.release.sd_hash)
        self.assertEqual(recovered.publication_state, PublicationState.RETRYING)
        self.assertEqual(recovered.publication_error_code, "interrupted")
        self.assertEqual(self.store.recover_interrupted_publications(), 0)

        keys = (
            (self.release.id, self.release.sd_hash),
            (alias.id, alias.sd_hash),
        )
        self.store.finish_publication(
            keys,
            state=PublicationState.DUPLICATE,
            outcome="artifact_duplicate",
            canonical=False,
            canonical_sha384="c" * 96,
            canonical_btih="f" * 40,
            canonical_torrent_url="https://index.example/torrents/f.torrent",
            canonical_magnet_uri="magnet:?xt=urn:btih:" + "f" * 40,
            winning_release_id=alias.id,
        )
        self.assertEqual(self.store.publication_counts(), {"duplicate": 2})
        entry = self.store.search_archive("Alias")[0][0]
        self.assertEqual(entry.publication_state, PublicationState.DUPLICATE)
        self.assertIn("f" * 40, entry.canonical_magnet_uri)

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

    def test_tracker_policy_cache_is_durable_and_scoped_to_its_endpoint(self) -> None:
        endpoint = "https://index.example/api/v2/torrents/tracker-policy/"
        self.assertIsNone(self.store.load_tracker_policy_cache(endpoint))

        cached = self.store.save_tracker_policy_cache(
            endpoint,
            '"version-one"',
            b'{"trackers":[]}',
        )

        self.assertEqual(cached.endpoint, endpoint)
        self.assertEqual(cached.etag, '"version-one"')
        self.assertEqual(cached.document, b'{"trackers":[]}')
        self.assertEqual(cached.updated_at, self.now)
        reopened = JobStore(self.store.path, clock=lambda: self.now)
        self.assertEqual(reopened.load_tracker_policy_cache(endpoint), cached)
        self.assertIsNone(
            reopened.load_tracker_policy_cache(
                "https://other.example/api/v2/torrents/tracker-policy/"
            )
        )

        self.now = 200
        replacement = self.store.save_tracker_policy_cache(
            "https://other.example/api/v2/torrents/tracker-policy/",
            '"version-two"',
            b'{"different":true}',
        )
        self.assertEqual(replacement.updated_at, 200)
        self.assertIsNone(self.store.load_tracker_policy_cache(endpoint))


if __name__ == "__main__":
    unittest.main()
