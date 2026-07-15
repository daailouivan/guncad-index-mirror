from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from guncadmirror.cancellation import AcquisitionCancelled
from guncadmirror.pipeline import CycleResult
from guncadmirror.runtime import Runtime, build_runtime
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
        )
        self.lbry = Mock()
        self.pipeline = Mock()
        self.stats = Mock()
        self.runtime = Runtime(self.settings, self.lbry, self.pipeline, self.stats)

    def test_start_prepares_storage_stats_and_lbry(self) -> None:
        self.runtime.start()
        self.assertTrue(self.settings.data_dir.is_dir())
        self.stats.start.assert_called_once_with()
        self.stats.set_state.assert_called_once_with("Waiting for LBRY")
        self.lbry.wait_until_ready.assert_called_once_with(
            self.settings.lbry_startup_timeout,
            stop=None,
        )
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
        self.pipeline.run_cycle.assert_called_once_with(None)
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
        self.stats.log.assert_any_call("Mirror cycle failed; see application log")
        stop.wait.assert_called_once_with(1)

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

    def test_stop_attempts_every_cleanup_after_an_error(self) -> None:
        self.stats.stop.side_effect = RuntimeError("thread stuck")
        with self.assertLogs("guncad-mirror", level="ERROR"):
            self.runtime.stop()
        self.pipeline.index_client.close.assert_called_once_with()
        self.lbry.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
