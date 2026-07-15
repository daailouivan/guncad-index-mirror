from __future__ import annotations

import signal
import unittest
from unittest.mock import Mock, patch

from guncadmirror.__main__ import main, parse_args
from guncadmirror.pipeline import CycleResult
from guncadmirror.settings import ConfigurationError, Settings


class ArgumentTests(unittest.TestCase):
    def test_parses_modes_and_rejects_nonpositive_release_cap(self) -> None:
        args = parse_args(["--once", "--verbose", "--max-releases", "3"])
        self.assertTrue(args.once)
        self.assertTrue(args.verbose)
        self.assertEqual(args.max_releases, 3)
        with self.assertRaises(SystemExit):
            parse_args(["--max-releases", "0"])


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(endpoint="https://index.example/api/v2/releases/")
        self.runtime = Mock()

    @patch("guncadmirror.__main__.signal.signal")
    @patch("guncadmirror.__main__.build_runtime")
    @patch("guncadmirror.__main__.Settings.from_env")
    def test_once_returns_failure_status_and_applies_cli_cap(
        self, from_env: Mock, build_runtime: Mock, signal_call: Mock
    ) -> None:
        from_env.return_value = self.settings
        build_runtime.return_value = self.runtime
        self.runtime.run_cycle.return_value = CycleResult(failed=1)

        self.assertEqual(main(["--once", "--max-releases", "2"]), 1)
        configured = build_runtime.call_args.args[0]
        self.assertEqual(configured.max_releases_per_run, 2)
        self.runtime.start.assert_called_once_with()
        self.runtime.stop.assert_called_once_with()
        self.assertEqual(signal_call.call_count, 2)

    @patch("guncadmirror.__main__.signal.signal")
    @patch("guncadmirror.__main__.build_runtime")
    @patch("guncadmirror.__main__.Settings.from_env")
    def test_once_success_returns_zero(
        self, from_env: Mock, build_runtime: Mock, _signal_call: Mock
    ) -> None:
        from_env.return_value = self.settings
        build_runtime.return_value = self.runtime
        self.runtime.run_cycle.return_value = CycleResult(ready=1)
        self.assertEqual(main(["--once"]), 0)

    @patch("guncadmirror.__main__.signal.signal")
    @patch("guncadmirror.__main__.build_runtime")
    @patch("guncadmirror.__main__.Settings.from_env")
    def test_forever_mode_passes_a_signal_aware_stop_event(
        self, from_env: Mock, build_runtime: Mock, signal_call: Mock
    ) -> None:
        from_env.return_value = self.settings
        build_runtime.return_value = self.runtime
        handlers: dict[int, object] = {}
        signal_call.side_effect = lambda number, handler: handlers.update(
            {number: handler}
        )

        def run_forever(stop: object) -> None:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            self.assertTrue(stop.is_set())

        self.runtime.run_forever.side_effect = run_forever
        with self.assertLogs("guncad-mirror", level="INFO"):
            self.assertEqual(main([]), 0)
        self.runtime.run_forever.assert_called_once()

    @patch("guncadmirror.__main__.Settings.from_env")
    def test_configuration_errors_return_two(self, from_env: Mock) -> None:
        from_env.side_effect = ConfigurationError("bad URL")
        with self.assertLogs("guncad-mirror", level="ERROR"):
            self.assertEqual(main([]), 2)

    @patch("guncadmirror.__main__.signal.signal")
    @patch("guncadmirror.__main__.build_runtime")
    @patch("guncadmirror.__main__.Settings.from_env")
    def test_keyboard_interrupt_is_distinct_from_fatal_failure(
        self, from_env: Mock, build_runtime: Mock, _signal_call: Mock
    ) -> None:
        from_env.return_value = self.settings
        build_runtime.return_value = self.runtime
        self.runtime.start.side_effect = KeyboardInterrupt
        with self.assertLogs("guncad-mirror", level="INFO"):
            self.assertEqual(main([]), 130)
        self.runtime.stop.assert_called_once_with()

        self.runtime.reset_mock()
        self.runtime.start.side_effect = RuntimeError("boom")
        with self.assertLogs("guncad-mirror", level="ERROR"):
            self.assertEqual(main([]), 1)
        self.runtime.stop.assert_called_once_with()

    @patch("guncadmirror.__main__.build_runtime")
    @patch("guncadmirror.__main__.Settings.from_env")
    def test_runtime_construction_failure_is_reported(
        self, from_env: Mock, build_runtime: Mock
    ) -> None:
        from_env.return_value = self.settings
        build_runtime.side_effect = OSError("state directory unavailable")
        with self.assertLogs("guncad-mirror", level="ERROR"):
            self.assertEqual(main([]), 1)


if __name__ == "__main__":
    unittest.main()
