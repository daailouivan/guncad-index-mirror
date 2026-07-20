from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from guncadmirror.cancellation import AcquisitionCancelled
from guncadmirror.pipeline import CycleResult
from guncadmirror.publication import PublicationCycleResult
from guncadmirror.runtime import Runtime, build_runtime
from guncadmirror.seeding import SeedingCycleResult
from guncadmirror.settings import Settings


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(
            endpoint="https://index.example/api/v2/releases/",
            data_dir=Path(self.temporary.name) / "data",
            min_free_space=0,
            loop_interval=1,
            cycle_error_interval=2,
        )
        self.lbry = Mock()
        self.pipeline = Mock()
        self.stats = Mock()
        self.runtime = Runtime(self.settings, self.lbry, self.pipeline, self.stats)

    def test_start_prepares_storage_and_stats_without_blocking_on_lbry(self) -> None:
        self.runtime.start()
        self.assertTrue(self.settings.data_dir.is_dir())
        self.stats.start.assert_called_once_with()
        self.stats.set_state.assert_not_called()
        self.lbry.wait_until_ready.assert_not_called()
        self.stats.log.assert_not_called()

    @patch("guncadmirror.runtime.start_webui")
    def test_start_optionally_launches_webui(self, start_webui: Mock) -> None:
        self.runtime.settings = Settings(
            endpoint=self.settings.endpoint,
            data_dir=self.settings.data_dir,
            enable_webui=True,
        )
        self.runtime.start()
        start_webui.assert_called_once_with(self.stats)

    def test_run_cycle_updates_operator_state(self) -> None:
        expected = CycleResult(discovered=3, ready=1, skipped=1, failed=1)
        self.pipeline.run_cycle.return_value = expected
        self.assertEqual(self.runtime.run_cycle(), expected)
        self.lbry.wait_until_ready.assert_called_once_with(
            self.settings.lbry_startup_timeout,
            stop=None,
        )
        self.pipeline.run_cycle.assert_called_once_with(None)
        self.stats.set_state.assert_any_call("Waiting for LBRY")
        self.stats.set_state.assert_any_call("Enumerating Index releases")
        self.stats.set_state.assert_any_call(
            "Cycle complete: 3 discovered, 1 ready, 1 skipped, 1 failed, 0 stopped"
        )
        self.stats.log.assert_called_once_with(
            "Cycle complete: 3 discovered, 1 ready, 1 skipped, 1 failed, 0 stopped",
            stdout=True,
        )

    def test_forever_loop_waits_and_stops_after_success(self) -> None:
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        self.pipeline.run_cycle.return_value = CycleResult()
        self.runtime.run_forever(stop)
        self.pipeline.run_cycle.assert_called_once_with(stop)
        stop.wait.assert_called_once_with(1)
        self.stats.set_state.assert_any_call("Sleeping for 1s")

    def test_forever_loop_survives_cycle_exception(self) -> None:
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        self.pipeline.run_cycle.side_effect = RuntimeError("Index down")
        with self.assertLogs("guncad-mirror", level="ERROR"):
            self.runtime.run_forever(stop)
        self.pipeline.run_cycle.assert_called_once_with(stop)
        self.stats.log.assert_any_call(
            "Mirror cycle failed; retrying in 2s; see application log"
        )
        stop.wait.assert_called_once_with(2)
        self.stats.set_state.assert_any_call("Sleeping for 2s")

    def test_forever_loop_treats_index_cancellation_as_a_clean_stop(self) -> None:
        stop = Mock()
        stop.is_set.return_value = False
        self.pipeline.run_cycle.side_effect = AcquisitionCancelled("stop")

        self.runtime.run_forever(stop)

        self.stats.log.assert_called_once_with(
            "Mirror stop requested during Index enumeration"
        )
        stop.wait.assert_not_called()

    def test_stop_and_runtime_builder_wire_components(self) -> None:
        self.runtime.odysee = Mock()
        self.runtime.stop()
        self.stats.stop.assert_called_once_with()
        self.pipeline.index_client.close.assert_called_once_with()
        self.lbry.close.assert_called_once_with()
        self.runtime.odysee.close.assert_called_once_with()

        built = build_runtime(self.settings)
        self.assertEqual(built.settings, self.settings)
        self.assertEqual(built.lbry.url, self.settings.lbry_url)
        self.assertEqual(built.pipeline.index_client.endpoint, self.settings.endpoint)
        self.assertEqual(built.pipeline.store.path, self.settings.state_path)
        self.assertIs(built.stats.store, built.pipeline.store)
        self.assertIsNotNone(built.odysee)
        self.assertIs(built.pipeline.fallback_acquirer, built.odysee)
        self.assertIs(built.pipeline.progress, built.stats)
        self.assertEqual(built.pipeline.record_event, built.stats.log)
        self.assertIs(built.pipeline.acquirer.progress, built.stats)
        self.assertIs(built.odysee.progress, built.stats)
        self.assertIsNone(built.publication)

    def test_publication_runs_before_lbry_and_after_new_artifacts(self) -> None:
        self.runtime.publication = Mock()
        self.runtime.publication.run.side_effect = [
            PublicationCycleResult(considered=2, attempted=1, published=1),
            PublicationCycleResult(considered=1, attempted=1, duplicates=1),
        ]
        self.pipeline.run_cycle.return_value = CycleResult(ready=1)

        self.runtime.run_cycle()

        self.assertEqual(self.runtime.publication.run.call_count, 2)
        self.assertEqual(self.lbry.wait_until_ready.call_count, 1)
        self.assertTrue(self.runtime.lbry_ready)
        self.assertEqual(
            [call.args[0] for call in self.stats.set_state.call_args_list[:2]],
            ["Publishing staged torrents to the Index", "Waiting for LBRY"],
        )
        self.assertTrue(
            any("1 attempted" in call.args[0] for call in self.stats.log.call_args_list)
        )

    def test_seeding_gates_each_publication_pass(self) -> None:
        calls: list[str] = []
        self.runtime.seeding = Mock()
        self.runtime.seeding.run.side_effect = lambda _stop: (
            calls.append("seed") or SeedingCycleResult(attempted=1, green=1)
        )
        self.runtime.publication = Mock()
        self.runtime.publication.run.side_effect = lambda _stop: (
            calls.append("publish") or PublicationCycleResult(attempted=1, published=1)
        )
        self.lbry.wait_until_ready.side_effect = lambda *_args, **_kwargs: calls.append(
            "lbry"
        )
        self.pipeline.run_cycle.side_effect = lambda _stop: (
            calls.append("pipeline") or CycleResult(ready=1)
        )

        self.runtime.run_cycle()

        self.assertEqual(
            calls,
            ["seed", "publish", "lbry", "pipeline", "seed", "publish"],
        )
        self.assertFalse(self.runtime.seeding_degraded)

    def test_qbittorrent_failure_closes_publication_gate(self) -> None:
        self.runtime.seeding = Mock()
        self.runtime.seeding.run.return_value = SeedingCycleResult(
            retrying=1,
            error_code="network_error",
            error="connection refused",
        )
        self.runtime.publication = Mock()
        self.pipeline.run_cycle.return_value = CycleResult()

        self.runtime.run_cycle()

        self.assertEqual(self.runtime.seeding.run.call_count, 2)
        self.runtime.publication.run.assert_not_called()
        self.assertTrue(self.runtime.seeding_degraded)

    def test_global_publication_pause_skips_second_pass_and_slows_retry(self) -> None:
        self.runtime.publication = Mock()
        self.runtime.publication.run.return_value = PublicationCycleResult(
            considered=2,
            attempted=1,
            retrying=1,
            paused=True,
        )
        self.runtime.publication.next_delay.return_value = 0
        self.pipeline.run_cycle.return_value = CycleResult()
        stop = Mock()
        stop.is_set.side_effect = [False, True]

        self.runtime.run_forever(stop)

        self.runtime.publication.run.assert_called_once_with(stop)
        stop.wait.assert_called_once_with(
            min(self.settings.loop_interval, self.settings.cycle_error_interval)
        )

    def test_stop_attempts_every_cleanup_after_an_error(self) -> None:
        self.runtime.publication = Mock()
        self.runtime.seeding = Mock()
        self.stats.stop.side_effect = RuntimeError("thread stuck")
        with self.assertLogs("guncad-mirror", level="ERROR"):
            self.runtime.stop()
        self.pipeline.index_client.close.assert_called_once_with()
        self.lbry.close.assert_called_once_with()
        self.runtime.seeding.close.assert_called_once_with()
        self.runtime.publication.close.assert_called_once_with()

    def test_qbittorrent_failure_uses_error_interval(self) -> None:
        self.runtime.seeding = Mock()
        self.runtime.seeding.run.return_value = SeedingCycleResult(
            retrying=1,
            error_code="network_error",
            error="connection refused",
        )
        self.runtime.seeding.next_delay.return_value = 0
        self.pipeline.run_cycle.return_value = CycleResult()
        stop = Mock()
        stop.is_set.side_effect = [False, True]

        self.runtime.run_forever(stop)

        stop.wait.assert_called_once_with(
            min(self.settings.loop_interval, self.settings.cycle_error_interval)
        )

    def test_runtime_builder_enables_authenticated_publication(self) -> None:
        settings = Settings(
            endpoint=self.settings.endpoint,
            data_dir=self.settings.data_dir,
            qbittorrent_enabled=True,
            qbittorrent_username="mirror",
            qbittorrent_password="secret",
            publish_enabled=True,
            publish_url="https://index.example/api/v2/torrents/publish/",
            publish_token="secret",
            publish_timeout=23,
        )

        built = build_runtime(settings)

        self.assertIsNotNone(built.publication)
        self.assertIsNotNone(built.seeding)
        self.assertEqual(built.seeding.client.url, settings.qbittorrent_url)
        self.assertEqual(built.publication.client.url, settings.publish_url)
        self.assertEqual(built.publication.client.timeout, 23)


if __name__ == "__main__":
    unittest.main()
