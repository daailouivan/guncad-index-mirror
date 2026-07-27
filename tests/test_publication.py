from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from guncadmirror import torrent as torrent_module
from guncadmirror.index_publisher import (
    CanonicalArtifact,
    PublicationPaused,
    PublicationResult,
    RetryablePublicationError,
)
from guncadmirror.models import (
    AcquisitionEvidence,
    AcquisitionTransport,
    PublicationState,
    Release,
)
from guncadmirror.publication import (
    PublicationPreparationError,
    PublicationScheduler,
    prepare_submission,
)
from guncadmirror.publisher import OutboxPublisher
from guncadmirror.settings import Settings
from guncadmirror.state import JobStore
from guncadmirror.torrent import bencode, create_torrent, parse_torrent
from guncadmirror.verification import hash_file

from .helpers import release_payload


class PublicationSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data"
        self.settings = Settings(
            endpoint="https://index.example/api/v2/releases/",
            data_dir=self.root,
            min_free_space=0,
            publish_enabled=True,
            publish_url="https://index.example/api/v2/torrents/publish/",
            publish_token="secret",
            publish_concurrency=1,
            retry_backoff=2,
        )
        self.now = 100.0
        self.store = JobStore(self.settings.state_path, clock=lambda: self.now)
        self.client = Mock()
        self.events: list[str] = []
        self.scheduler = PublicationScheduler(
            self.settings,
            self.store,
            self.client,
            record_event=self.events.append,
        )

    def complete(
        self,
        content: bytes,
        *,
        release_id: str,
        sd_hash: str,
        popularity: float = 1.0,
        file_name: str = "payload.zip",
        acquisition: AcquisitionTransport = AcquisitionTransport.LBRY,
    ) -> Release:
        raw = release_payload(
            content,
            release_id=release_id,
            sd_hash=sd_hash,
            name=f"Release {release_id[:4]}",
        )
        raw["origin"]["popularity"] = popularity
        release = Release.from_api(raw)
        payload = self.settings.releases_dir / release.id / file_name
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(content)
        hashes = hash_file(payload)
        torrent_path = (
            self.settings.outbox_dir
            / release.id
            / release.sd_hash
            / f"{hashes.sha384}.torrent"
        )
        torrent = create_torrent(payload, torrent_path, piece_length=16384)
        OutboxPublisher(self.settings.outbox_dir).publish(
            release,
            hashes,
            torrent,
            AcquisitionEvidence(
                acquisition,
                source_url=(
                    "https://player.odycdn.com/v6/streams/source/file"
                    if acquisition is AcquisitionTransport.ODYSEE_CDN
                    else None
                ),
                lbry_failure=(
                    "LbryError: no peers"
                    if acquisition is AcquisitionTransport.ODYSEE_CDN
                    else None
                ),
            ),
        )
        self.store.register(release)
        self.store.start_attempt(release)
        self.store.mark_verified(
            release,
            file_path=payload,
            sha384=hashes.sha384,
            sha256=hashes.sha256,
        )
        self.store.mark_awaiting_index(release, torrent)
        self.store.mark_seed_green(
            release.id,
            release.sd_hash,
            client_version="v5.2.3",
            observed_state="forcedUP",
            content_path=f"/downloads/releases/{release.id}/{file_name}",
            dht_nodes=1,
            working_trackers=0,
            recheck_interval=300,
        )
        return release

    def result(
        self,
        candidate: Release,
        *,
        state: PublicationState = PublicationState.PUBLISHED,
        outcome: str = "created",
        canonical_btih: str | None = None,
        canonical_sha384: str | None = None,
    ) -> PublicationResult:
        job = self.store.get(candidate.id, candidate.sd_hash)
        canonical_btih = canonical_btih or job.info_hash
        canonical_sha384 = canonical_sha384 or job.sha384
        return PublicationResult(
            state=state,
            outcome=outcome,
            canonical=canonical_btih == job.info_hash,
            artifact=CanonicalArtifact(
                sha384=canonical_sha384,
                btih=canonical_btih,
                torrent_url=f"https://index.example/torrents/{canonical_btih}/",
                magnet_uri=f"magnet:?xt=urn:btih:{canonical_btih}",
                winning_release_id=candidate.id,
            ),
        )

    def test_prepares_sanitized_wire_manifest_from_staged_evidence(self) -> None:
        release = self.complete(
            b"payload",
            release_id="a" * 40,
            sd_hash="b" * 96,
            acquisition=AcquisitionTransport.ODYSEE_CDN,
        )
        candidate = self.store.publication_candidates()[0]

        submission = prepare_submission(self.settings, candidate)
        document = json.loads(submission.manifest)

        self.assertEqual(submission.release_id, release.id)
        self.assertEqual(submission.sd_hash, release.sd_hash)
        self.assertEqual(document["schema"], "guncad-mirror-publication-v1")
        self.assertEqual(document["release"], {"id": release.id})
        self.assertEqual(document["acquisition"], {"transport": "odysee-cdn"})
        self.assertEqual(document["artifact"]["file_name"], "payload.zip")
        self.assertEqual(document["torrent"]["file_name"], "payload.zip")
        self.assertEqual(document["torrent"]["btih"], submission.btih)
        self.assertEqual(submission.torrent, candidate.job.torrent_path.read_bytes())
        self.assertEqual(document["torrent"]["trackers"], [])

    def test_preparation_strips_legacy_trackers_without_changing_btih(self) -> None:
        release = self.complete(
            b"legacy trackers",
            release_id="a" * 40,
            sd_hash="b" * 96,
        )
        candidate = self.store.publication_candidates()[0]
        torrent_path = candidate.job.torrent_path
        self.assertIsNotNone(torrent_path)
        tracker = "udp://tracker.example:80/announce"
        original = torrent_path.read_bytes()
        decoder = torrent_module._BencodeDecoder(original)
        metainfo = decoder.decode()
        metainfo[b"announce"] = tracker.encode()
        metainfo[b"announce-list"] = [[tracker.encode()]]
        legacy = bencode(metainfo)
        torrent_path.write_bytes(legacy)
        legacy_parsed = parse_torrent(legacy)
        self.store._update(  # noqa: SLF001 - exercise an upgrade-era durable row
            release,
            magnet_uri=legacy_parsed.magnet_uri,
        )
        manifest_path = (
            self.settings.outbox_dir / release.id / release.sd_hash / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text())
        manifest["torrent"]["magnet_uri"] = legacy_parsed.magnet_uri
        manifest["torrent"]["trackers"] = [tracker]
        manifest["torrent"]["sha256"] = legacy_parsed.torrent_sha256
        manifest_path.write_text(json.dumps(manifest))

        submission = prepare_submission(
            self.settings,
            self.store.publication_candidates()[0],
        )
        submitted = parse_torrent(submission.torrent)
        document = json.loads(submission.manifest)

        self.assertEqual(submitted.info_hash, legacy_parsed.info_hash)
        self.assertEqual(submitted.trackers, ())
        self.assertEqual(document["torrent"]["trackers"], [])
        self.assertEqual(document["torrent"]["magnet_uri"], submitted.magnet_uri)
        self.assertEqual(
            document["torrent"]["sha256"],
            submitted.torrent_sha256,
        )

    def test_preparation_accepts_legacy_manifest_as_direct_lbry(self) -> None:
        release = self.complete(
            b"legacy",
            release_id="a" * 40,
            sd_hash="b" * 96,
        )
        manifest_path = (
            self.settings.outbox_dir / release.id / release.sd_hash / "manifest.json"
        )
        original = manifest_path.read_text()
        document = json.loads(original)
        del document["acquisition"]
        manifest_path.write_text(json.dumps(document))

        submission = prepare_submission(
            self.settings,
            self.store.publication_candidates()[0],
        )

        self.assertEqual(
            json.loads(submission.manifest)["acquisition"],
            {"transport": "lbry"},
        )

    def test_preparation_fails_closed_on_missing_or_contradictory_artifacts(
        self,
    ) -> None:
        release = self.complete(
            b"payload",
            release_id="a" * 40,
            sd_hash="b" * 96,
        )
        candidate = self.store.publication_candidates()[0]
        manifest_path = (
            self.settings.outbox_dir / release.id / release.sd_hash / "manifest.json"
        )
        original = manifest_path.read_text()
        document = json.loads(original)
        document["artifact"]["sha384"] = "f" * 96
        manifest_path.write_text(json.dumps(document))
        with self.assertRaisesRegex(PublicationPreparationError, "contradicts"):
            prepare_submission(self.settings, candidate)

        manifest_path.unlink()
        with self.assertRaisesRegex(PublicationPreparationError, "missing"):
            prepare_submission(self.settings, candidate)

        manifest_path.write_text(original)
        candidate.job.torrent_path.write_bytes(b"garbage")
        with self.assertRaisesRegex(PublicationPreparationError, "parsed"):
            prepare_submission(self.settings, candidate)

    def test_highest_popularity_publishes_first_and_exact_sd_alias_posts_once(
        self,
    ) -> None:
        content = b"same payload"
        leader = self.complete(
            content,
            release_id="1" * 40,
            sd_hash="a" * 96,
            popularity=3,
        )
        alias = self.complete(
            content,
            release_id="2" * 40,
            sd_hash="a" * 96,
            popularity=2,
        )
        lower = self.complete(
            content,
            release_id="3" * 40,
            sd_hash="b" * 96,
            popularity=1,
            file_name="other.zip",
        )
        leader_result = self.result(leader)
        lower_result = self.result(
            lower,
            state=PublicationState.DUPLICATE,
            outcome="artifact_duplicate",
            canonical_btih=leader_result.artifact.btih,
        )
        self.client.publish.side_effect = [leader_result, lower_result]

        cycle = self.scheduler.run()

        self.assertEqual(cycle.considered, 3)
        self.assertEqual(cycle.attempted, 2)
        self.assertEqual(cycle.published, 2)
        self.assertEqual(cycle.duplicates, 1)
        calls = [call.args[0].release_id for call in self.client.publish.call_args_list]
        self.assertEqual(calls, [leader.id, lower.id])
        self.assertEqual(
            self.store.get(alias.id, alias.sd_hash).publication_state,
            PublicationState.PUBLISHED,
        )
        self.assertEqual(
            self.store.get(lower.id, lower.sd_hash).publication_state,
            PublicationState.DUPLICATE,
        )
        self.assertTrue(any("Published" in event for event in self.events))
        self.assertTrue(any("existing canonical" in event for event in self.events))

    def test_same_descriptor_with_different_btih_is_not_coalesced(self) -> None:
        content = b"same payload"
        leader = self.complete(
            content,
            release_id="1" * 40,
            sd_hash="a" * 96,
            popularity=2,
        )
        divergent = self.complete(
            content,
            release_id="2" * 40,
            sd_hash="a" * 96,
            popularity=1,
            file_name="renamed.zip",
        )
        self.client.publish.side_effect = [
            self.result(leader),
            PublicationResult(
                state=PublicationState.CONFLICT,
                outcome=None,
                canonical=None,
                artifact=self.result(leader).artifact,
                error_code="sd_hash_conflict",
                error_message="Descriptor already committed",
            ),
        ]

        cycle = self.scheduler.run()

        self.assertEqual(cycle.attempted, 2)
        self.assertEqual(cycle.published, 1)
        self.assertEqual(cycle.conflicts, 1)
        calls = [call.args[0].release_id for call in self.client.publish.call_args_list]
        self.assertEqual(calls, [leader.id, divergent.id])
        self.assertEqual(
            self.store.get(divergent.id, divergent.sd_hash).publication_state,
            PublicationState.CONFLICT,
        )

    def test_retryable_failure_blocks_lower_priority_same_sha(self) -> None:
        content = b"same payload"
        leader = self.complete(
            content,
            release_id="1" * 40,
            sd_hash="a" * 96,
            popularity=2,
        )
        lower = self.complete(
            content,
            release_id="2" * 40,
            sd_hash="b" * 96,
            popularity=1,
        )
        self.client.publish.side_effect = RetryablePublicationError(
            "http_429",
            "rate limited",
            retry_after=30,
        )

        cycle = self.scheduler.run()

        self.assertEqual(cycle.attempted, 1)
        self.assertEqual(cycle.retrying, 1)
        retrying = self.store.get(leader.id, leader.sd_hash)
        self.assertEqual(retrying.publication_state, PublicationState.RETRYING)
        self.assertEqual(retrying.publication_next_attempt_at, 130)
        self.assertEqual(
            self.store.get(lower.id, lower.sd_hash).publication_state,
            PublicationState.PENDING,
        )
        self.assertEqual(self.scheduler.next_delay(), 30)
        self.client.publish.reset_mock()
        self.assertEqual(self.scheduler.run().attempted, 0)
        self.client.publish.assert_not_called()

    def test_global_pause_stops_other_sha_groups_without_rejecting_them(self) -> None:
        first = self.complete(
            b"first",
            release_id="1" * 40,
            sd_hash="a" * 96,
        )
        second = self.complete(
            b"second",
            release_id="2" * 40,
            sd_hash="b" * 96,
        )
        self.client.publish.side_effect = PublicationPaused("http_401", "bad token")

        cycle = self.scheduler.run()

        self.assertTrue(cycle.paused)
        self.assertEqual(cycle.attempted, 1)
        states = {
            self.store.get(first.id, first.sd_hash).publication_state,
            self.store.get(second.id, second.sd_hash).publication_state,
        }
        self.assertEqual(states, {PublicationState.RETRYING, PublicationState.PENDING})
        self.assertTrue(any("paused" in event for event in self.events))

    def test_terminal_rejection_and_conflict_are_operator_visible(self) -> None:
        rejected = self.complete(
            b"rejected",
            release_id="1" * 40,
            sd_hash="a" * 96,
        )
        self.client.publish.return_value = PublicationResult(
            state=PublicationState.REJECTED,
            outcome=None,
            canonical=None,
            artifact=None,
            error_code="unknown_sd_hash",
            error_message="No current origin",
        )
        cycle = self.scheduler.run()
        self.assertEqual(cycle.rejected, 1)
        self.assertEqual(
            self.store.get(rejected.id, rejected.sd_hash).publication_state,
            PublicationState.REJECTED,
        )

        conflict = self.complete(
            b"conflict",
            release_id="2" * 40,
            sd_hash="b" * 96,
        )
        self.client.publish.return_value = PublicationResult(
            state=PublicationState.CONFLICT,
            outcome=None,
            canonical=None,
            artifact=None,
            error_code="sd_hash_conflict",
            error_message="Descriptor already committed",
        )
        cycle = self.scheduler.run()
        self.assertEqual(cycle.conflicts, 1)
        self.assertEqual(
            self.store.get(conflict.id, conflict.sd_hash).publication_state,
            PublicationState.CONFLICT,
        )
        self.assertTrue(any("CONFLICT" in event for event in self.events))

    def test_disabled_empty_cancelled_and_close_paths_are_noops(self) -> None:
        disabled = PublicationScheduler(
            Settings(
                endpoint=self.settings.endpoint,
                data_dir=self.root,
                publish_enabled=False,
            ),
            self.store,
            self.client,
        )
        self.assertEqual(disabled.run().considered, 0)
        self.assertEqual(self.scheduler.run().considered, 0)

        self.complete(
            b"payload",
            release_id="a" * 40,
            sd_hash="b" * 96,
        )
        stop = Mock()
        stop.is_set.return_value = True
        self.assertEqual(self.scheduler.run(stop).attempted, 0)
        self.scheduler.close()
        self.client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
