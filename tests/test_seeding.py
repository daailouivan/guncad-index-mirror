from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, call

from guncadmirror.models import SeedingState
from guncadmirror.qbittorrent import (
    QBitConfigurationError,
    QBitObservation,
    QBitRetryableError,
    QBitTorrent,
    QBitTracker,
    QBitTransfer,
)
from guncadmirror.seeding import SeedingScheduler, prepare_seed
from guncadmirror.settings import Settings
from guncadmirror.state import JobStore
from guncadmirror.torrent import create_torrent

from .helpers import make_release


class SeedingSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "data"
        self.settings = Settings(
            endpoint="https://index.example/api/v2/releases/",
            data_dir=self.data_dir,
            min_free_space=0,
            qbittorrent_enabled=True,
            qbittorrent_url="http://qbittorrent:8080",
            qbittorrent_username="mirror",
            qbittorrent_password="secret",
            qbittorrent_ready_timeout=2,
            qbittorrent_poll_interval=1,
            qbittorrent_recheck_interval=60,
        )
        self.now = 100.0
        self.tick = 10.0
        self.store = JobStore(
            self.settings.state_path,
            clock=lambda: self.now,
        )
        self.release = make_release()
        self.payload = self.settings.releases_dir / "payload.zip"
        self.payload.parent.mkdir(parents=True)
        self.payload.write_bytes(b"payload")
        self.torrent = create_torrent(
            self.payload,
            self.settings.outbox_dir
            / self.release.id
            / self.release.sd_hash
            / "payload.torrent",
            piece_length=16 * 1024,
        )
        self.store.register(self.release)
        self.store.start_attempt(self.release)
        self.store.mark_verified(
            self.release,
            file_path=self.payload,
            sha384=hashlib.sha384(b"payload").hexdigest(),
            sha256=hashlib.sha256(b"payload").hexdigest(),
        )
        self.store.mark_awaiting_index(self.release, self.torrent)
        self.client = Mock()
        self.client.versions.return_value = ("v5.2.3", "2.15.1")
        self.events: list[str] = []
        self.scheduler = SeedingScheduler(
            self.settings,
            self.store,
            self.client,
            record_event=self.events.append,
            monotonic=lambda: self.tick,
            sleep=self._sleep,
        )

    def _sleep(self, delay: float) -> None:
        self.tick += delay

    def observation(
        self,
        *,
        trackers: tuple[QBitTracker, ...] = (),
        dht_nodes: int = 4,
        **overrides: object,
    ) -> QBitObservation:
        torrent_values = {
            "info_hash": self.torrent.info_hash,
            "content_path": "/downloads/releases/payload.zip",
            "save_path": "/downloads/releases",
            "progress": 1.0,
            "amount_left": 0,
            "total_size": len(b"payload"),
            "state": "forcedUP",
            "force_start": True,
            "category": "guncad-mirror",
            "tags": ("guncad-mirror",),
        }
        torrent_values.update(overrides)
        return QBitObservation(
            torrent=QBitTorrent(**torrent_values),
            transfer=QBitTransfer("firewalled", dht_nodes),
            trackers=trackers,
        )

    def test_adds_forces_announces_and_marks_exact_torrent_green(self) -> None:
        self.client.observe.side_effect = [None, self.observation()]

        result = self.scheduler.run()

        self.assertEqual(result.considered, 1)
        self.assertEqual(result.attempted, 1)
        self.assertEqual(result.green, 1)
        self.client.add.assert_called_once_with(
            self.torrent.torrent_path,
            save_path="/downloads/releases",
            category="guncad-mirror",
            tag="guncad-mirror",
        )
        self.client.force_start.assert_called_once_with(self.torrent.info_hash)
        self.client.reannounce.assert_called_once_with(self.torrent.info_hash)
        job = self.store.get(self.release.id, self.release.sd_hash)
        self.assertEqual(job.seeding_state, SeedingState.GREEN)
        self.assertEqual(job.seeding_client_version, "v5.2.3")
        self.assertEqual(job.seeding_observed_state, "forcedUP")
        self.assertEqual(job.seeding_dht_nodes, 4)
        self.assertEqual(job.seeding_next_attempt_at, 160)
        self.assertEqual(len(self.store.publication_candidates()), 1)
        self.assertTrue(any("SEED GREEN" in event for event in self.events))

        self.client.reset_mock()
        fresh = self.scheduler.run()
        self.assertEqual(fresh.considered, 1)
        self.assertEqual(fresh.attempted, 0)
        self.client.versions.assert_not_called()

    def test_existing_green_torrent_is_idempotent_and_revalidated(self) -> None:
        self.client.observe.return_value = self.observation()
        first = self.scheduler.run()
        self.assertEqual(first.green, 1)
        self.client.add.assert_not_called()
        self.client.force_start.assert_not_called()

        self.now = 160
        second = self.scheduler.run()
        self.assertEqual(second.green, 1)
        self.assertEqual(self.client.observe.call_count, 2)
        self.assertEqual(
            sum("SEED GREEN" in event for event in self.events),
            1,
        )

    def test_declaratively_reconciles_only_owned_torrent_trackers(self) -> None:
        stale = "udp://stale.example:80/announce"
        operator = "udp://operator.example:80/announce"
        index = "https://index.example/announce"
        policy = Mock(
            desired_trackers=(index, operator),
            removals_authoritative=True,
        )
        self.scheduler.tracker_policy = policy
        self.client.observe.side_effect = (
            self.observation(
                trackers=(
                    QBitTracker("** [DHT] **", 0, -1),
                    QBitTracker(stale, 2, 0),
                    QBitTracker(operator, 0, 1),
                )
            ),
            self.observation(
                trackers=(
                    QBitTracker("** [DHT] **", 0, -1),
                    QBitTracker(index, 0, 0),
                    QBitTracker(operator, 0, 1),
                )
            ),
        )

        result = self.scheduler.run()

        self.assertEqual(result.green, 1)
        self.assertEqual(result.tracker_updates, 1)
        self.assertEqual(result.tracker_errors, 0)
        self.client.remove_trackers.assert_called_once_with(
            self.torrent.info_hash,
            (stale,),
        )
        self.client.add_trackers.assert_called_once_with(
            self.torrent.info_hash,
            (index,),
        )
        self.assertLess(
            self.client.method_calls.index(
                call.add_trackers(self.torrent.info_hash, (index,))
            ),
            self.client.method_calls.index(
                call.remove_trackers(self.torrent.info_hash, (stale,))
            ),
        )
        self.assertEqual(self.client.reannounce.call_count, 2)

        self.now = 160
        self.client.reset_mock()
        self.client.observe.side_effect = None
        self.client.observe.return_value = self.observation(
            category="personal",
            trackers=(QBitTracker(stale, 2, 0),),
        )
        result = self.scheduler.run()
        self.assertEqual(result.green, 1)
        self.assertEqual(result.tracker_updates, 0)
        self.client.remove_trackers.assert_not_called()
        self.client.add_trackers.assert_not_called()

    def test_unknown_remote_policy_adds_operator_hints_without_removing(self) -> None:
        policy = Mock(
            desired_trackers=("udp://operator.example:80/announce",),
            removals_authoritative=False,
        )
        self.scheduler.tracker_policy = policy
        self.client.observe.side_effect = (
            self.observation(
                trackers=(QBitTracker("udp://existing.example:80/announce", 2, 0),)
            ),
            self.observation(
                trackers=(
                    QBitTracker("udp://existing.example:80/announce", 2, 0),
                    QBitTracker("udp://operator.example:80/announce", 0, 1),
                )
            ),
        )

        result = self.scheduler.run()

        self.assertEqual(result.tracker_updates, 1)
        self.client.remove_trackers.assert_not_called()
        self.client.add_trackers.assert_called_once_with(
            self.torrent.info_hash,
            ("udp://operator.example:80/announce",),
        )

    def test_successful_tracker_removal_requires_fresh_discovery_evidence(
        self,
    ) -> None:
        stale = "udp://stale.example:80/announce"
        self.scheduler.tracker_policy = Mock(
            desired_trackers=(),
            removals_authoritative=True,
        )
        before = self.observation(
            trackers=(QBitTracker(stale, 2, 0),),
            dht_nodes=0,
        )
        after = self.observation(dht_nodes=0)
        self.client.observe.side_effect = (before, after, after, after)

        result = self.scheduler.run()

        self.assertEqual(result.green, 0)
        self.assertEqual(result.retrying, 1)
        self.client.remove_trackers.assert_called_once_with(
            self.torrent.info_hash,
            (stale,),
        )
        job = self.store.get(self.release.id, self.release.sd_hash)
        self.assertEqual(job.seeding_error_code, "not_green")

    def test_tracker_mutation_failure_never_closes_publication_gate(self) -> None:
        policy = Mock(
            desired_trackers=("https://index.example/announce",),
            removals_authoritative=True,
        )
        self.scheduler.tracker_policy = policy
        self.client.observe.return_value = self.observation()
        self.client.add_trackers.side_effect = QBitConfigurationError(
            "http_404",
            "unsupported tracker endpoint",
        )

        first = self.scheduler.run()

        self.assertEqual(first.green, 1)
        self.assertEqual(first.tracker_updates, 0)
        self.assertEqual(first.tracker_errors, 1)
        self.assertEqual(len(self.store.publication_candidates()), 1)
        self.assertEqual(
            sum("TRACKER RECONCILIATION DEGRADED" in event for event in self.events),
            1,
        )

        self.now = 160
        second = self.scheduler.run()
        self.assertEqual(second.green, 1)
        self.assertEqual(
            sum("TRACKER RECONCILIATION DEGRADED" in event for event in self.events),
            1,
        )

        self.now = 220
        self.client.add_trackers.side_effect = None
        self.client.observe.side_effect = (
            self.observation(),
            self.observation(
                trackers=(QBitTracker("https://index.example/announce", 0, 0),)
            ),
        )
        recovered = self.scheduler.run()
        self.assertEqual(recovered.tracker_updates, 1)
        self.assertIn("TRACKER RECONCILIATION RECOVERED", self.events)

    def test_identical_btih_can_seed_verified_duplicate_release_paths(self) -> None:
        alias = make_release(
            release_id="c" * 40,
            sd_hash="d" * 96,
        )
        alias_payload = self.settings.releases_dir / "alias" / "payload.zip"
        alias_payload.parent.mkdir(parents=True)
        alias_payload.write_bytes(b"payload")
        alias_torrent = create_torrent(
            alias_payload,
            self.settings.outbox_dir / alias.id / alias.sd_hash / "payload.torrent",
            piece_length=16 * 1024,
        )
        self.assertEqual(alias_torrent.info_hash, self.torrent.info_hash)
        self.store.register(alias)
        self.store.start_attempt(alias)
        self.store.mark_verified(
            alias,
            file_path=alias_payload,
            sha384=hashlib.sha384(b"payload").hexdigest(),
            sha256=hashlib.sha256(b"payload").hexdigest(),
        )
        self.store.mark_awaiting_index(alias, alias_torrent)
        self.client.observe.return_value = self.observation(
            content_path="/downloads/releases/alias/payload.zip",
            save_path="/downloads/releases/alias",
        )

        result = self.scheduler.run()

        self.assertEqual(result.considered, 2)
        self.assertEqual(result.green, 2)
        self.client.add.assert_not_called()
        self.assertEqual(
            self.store.get(self.release.id, self.release.sd_hash).seeding_content_path,
            "/downloads/releases/alias/payload.zip",
        )
        self.assertEqual(
            self.store.get(alias.id, alias.sd_hash).seeding_state,
            SeedingState.GREEN,
        )

    def test_lost_seed_readiness_is_retryable_and_closes_publication_gate(self) -> None:
        self.client.observe.return_value = self.observation()
        self.scheduler.run()
        self.now = 160
        self.client.observe.side_effect = QBitRetryableError(
            "network_error", "connection reset"
        )

        result = self.scheduler.run()

        self.assertEqual(result.retrying, 1)
        job = self.store.get(self.release.id, self.release.sd_hash)
        self.assertEqual(job.seeding_state, SeedingState.RETRYING)
        self.assertEqual(job.seeding_error_code, "network_error")
        self.assertEqual(self.store.publication_candidates(), [])
        self.assertTrue(any("SEED LOST" in event for event in self.events))

    def test_path_size_and_local_artifact_conflicts_are_blocked(self) -> None:
        cases = (
            self.observation(content_path="/downloads/elsewhere/payload.zip"),
            self.observation(save_path="/downloads/elsewhere"),
            self.observation(total_size=99),
            self.observation(
                progress=0.5,
                amount_left=3,
                state="downloading",
                force_start=False,
            ),
        )
        for observation in cases:
            with self.subTest(observation=observation):
                self.store.mark_awaiting_index(self.release, self.torrent)
                self.client.observe.side_effect = None
                self.client.observe.return_value = observation
                result = self.scheduler.run()
                self.assertEqual(result.blocked, 1)
                self.assertEqual(
                    self.store.get(self.release.id, self.release.sd_hash).seeding_state,
                    SeedingState.BLOCKED,
                )

        self.store.mark_awaiting_index(self.release, self.torrent)
        self.payload.unlink()
        result = self.scheduler.run()
        self.assertEqual(result.blocked, 1)
        self.client.add.assert_not_called()

    def test_non_green_torrent_times_out_without_becoming_publishable(self) -> None:
        incomplete = self.observation(
            progress=1.0,
            amount_left=0,
            state="stoppedUP",
            force_start=False,
        )
        self.client.observe.return_value = incomplete

        result = self.scheduler.run()

        self.assertEqual(result.retrying, 1)
        self.assertGreaterEqual(self.client.observe.call_count, 2)
        self.client.force_start.assert_called_once_with(self.torrent.info_hash)
        job = self.store.get(self.release.id, self.release.sd_hash)
        self.assertEqual(job.seeding_state, SeedingState.RETRYING)
        self.assertEqual(job.seeding_error_code, "not_green")
        self.assertIn("state=stoppedUP", job.seeding_error)

    def test_configuration_failure_pauses_and_unavailable_client_defers(self) -> None:
        self.client.versions.side_effect = QBitConfigurationError(
            "authentication_failed", "bad credentials"
        )
        paused = self.scheduler.run()
        self.assertTrue(paused.paused)
        self.assertEqual(paused.error_code, "authentication_failed")
        self.assertEqual(
            self.store.get(self.release.id, self.release.sd_hash).seeding_state,
            SeedingState.PENDING,
        )

        self.client.versions.side_effect = QBitRetryableError(
            "network_error", "refused"
        )
        deferred = self.scheduler.run()
        self.assertFalse(deferred.paused)
        self.assertEqual(deferred.retrying, 1)
        self.assertEqual(deferred.attempted, 0)

    def test_prepare_seed_checks_torrent_and_maps_container_path(self) -> None:
        candidate = self.store.seeding_candidates()[0]
        paths = prepare_seed(self.settings, candidate)
        self.assertEqual(paths.payload_path, self.payload.resolve())
        self.assertEqual(paths.qbit_content_path, "/downloads/releases/payload.zip")
        self.assertEqual(paths.qbit_save_path, "/downloads/releases")

        self.torrent.torrent_path.write_bytes(b"invalid")
        with self.assertRaisesRegex(Exception, "torrent cannot be parsed"):
            prepare_seed(self.settings, candidate)

    def test_stopped_run_is_side_effect_free_and_close_closes_client(self) -> None:
        stop = Event()
        stop.set()
        result = self.scheduler.run(stop)
        self.assertEqual(result.considered, 1)
        self.assertEqual(result.attempted, 0)
        self.client.versions.assert_not_called()

        disabled = SeedingScheduler(
            Settings(endpoint=self.settings.endpoint, data_dir=self.data_dir),
            self.store,
            self.client,
        )
        self.assertEqual(disabled.run().considered, 0)
        self.assertIsNone(disabled.next_delay())
        self.scheduler.close()
        self.client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
