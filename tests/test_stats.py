from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from guncadmirror.settings import Settings
from guncadmirror.state import JobStore
from guncadmirror.stats import StatsCollector, directory_size


class StatsCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = Settings(
            endpoint="https://index.example/api/v2/releases/",
            data_dir=self.root,
            enable_webui=True,
        )
        self.store = JobStore(self.settings.state_path)

    @patch("guncadmirror.stats.psutil.disk_usage", return_value="disk")
    @patch("guncadmirror.stats.psutil.net_io_counters", return_value="net")
    @patch("guncadmirror.stats.psutil.virtual_memory")
    @patch("guncadmirror.stats.psutil.cpu_percent", return_value=12.5)
    def test_collects_state_jobs_system_values_and_events(
        self,
        cpu: Mock,
        memory: Mock,
        network: Mock,
        disk: Mock,
    ) -> None:
        memory.return_value.percent = 42.0
        collector = StatsCollector(self.settings, self.store)
        collector.set_state("Working")
        with self.assertLogs("guncad-mirror", level="INFO"):
            collector.log("hello", stdout=True)
        collector.collect()
        collector.collect_disk()
        snapshot = collector.snapshot()

        self.assertEqual(snapshot["mirror_state"], "Working")
        self.assertEqual(snapshot["psutil_cpu"], 12.5)
        self.assertEqual(snapshot["psutil_mem"], 42.0)
        self.assertEqual(snapshot["psutil_net"], "net")
        self.assertEqual(snapshot["psutil_disk"], "disk")
        self.assertEqual(snapshot["known_jobs"], 0)
        self.assertEqual(snapshot["mirror_api_max_pages"], 1000)
        self.assertIsNone(snapshot["mirror_max_releases_per_run"])
        self.assertEqual(snapshot["mirror_lbry_url"], "http://127.0.0.1:5279")
        self.assertEqual(snapshot["mirror_blacklisted_handles"], ())
        self.assertEqual(snapshot["mirror_releases_dir"], str(self.root / "releases"))
        self.assertEqual(snapshot["mirror_outbox_dir"], str(self.root / "outbox"))
        self.assertIn("hello", snapshot["extralog"][0])
        cpu.assert_called_once_with(interval=None)
        disk.assert_called_once_with(self.root)

    @patch("guncadmirror.stats.psutil.disk_usage", return_value=Mock())
    @patch("guncadmirror.stats.psutil.net_io_counters", return_value=Mock())
    @patch(
        "guncadmirror.stats.psutil.virtual_memory",
        return_value=Mock(percent=0),
    )
    @patch("guncadmirror.stats.psutil.cpu_percent", return_value=0)
    def test_start_and_stop_manage_daemon_collectors(
        self, _cpu: Mock, _memory: Mock, _network: Mock, _disk: Mock
    ) -> None:
        collector = StatsCollector(
            self.settings, self.store, cheap_interval=60, disk_interval=60
        )
        collector.start()
        self.assertEqual(
            [thread.name for thread in collector._threads],
            [
                "mirror-stats",
                "mirror-disk-stats",
            ],
        )
        self.assertTrue(all(thread.daemon for thread in collector._threads))
        collector.stop()
        self.assertTrue(collector._stop.is_set())

    def test_collector_loop_logs_and_survives_probe_failure(self) -> None:
        collector = StatsCollector(self.settings, self.store)
        collector._stop.wait = Mock(side_effect=[False, True])
        target = Mock(side_effect=RuntimeError("probe failed"))
        with self.assertLogs("guncad-mirror.stats", level="ERROR") as logs:
            collector._run_collector(target, 1)
        self.assertIn("Statistics collection failed", logs.output[0])
        target.assert_called_once_with()

    def test_directory_size_ignores_files_that_disappear(self) -> None:
        (self.root / "one").write_bytes(b"123")
        (self.root / "two").write_bytes(b"4567")
        self.assertGreaterEqual(directory_size(self.root), 7)

        real_stat = Path.stat

        def flaky_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
            if path.name == "two":
                raise FileNotFoundError(path)
            return real_stat(path, *args, **kwargs)

        with patch(
            "guncadmirror.stats.Path.stat", autospec=True, side_effect=flaky_stat
        ):
            self.assertGreaterEqual(directory_size(self.root), 3)


if __name__ == "__main__":
    unittest.main()
