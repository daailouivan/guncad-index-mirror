from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from guncadmirror.webui import create_app, start


class WebUiTests(unittest.TestCase):
    def setUp(self) -> None:
        disk = SimpleNamespace(percent=25, free=10 * 1024**3)
        network = SimpleNamespace(
            bytes_sent=1,
            bytes_recv=2,
            errout=0,
            errin=0,
            dropout=0,
            dropin=0,
        )
        self.collector = Mock()
        self.collector.snapshot.return_value = {
            "version": "test-ref",
            "mirror_state": "Sleeping",
            "mirror_api_endpoint": "https://index.example/api/releases/",
            "mirror_enable_webui": True,
            "mirror_release_max_size": 1024,
            "mirror_min_free_space": 512,
            "mirror_api_max_pages": 2,
            "mirror_torrent_piece_length": 1024**2,
            "disk_space_used": 123,
            "job_counts": {"awaiting_index": 2},
            "known_jobs": 2,
            "psutil_cpu": 1,
            "psutil_mem": 2,
            "psutil_disk": disk,
            "psutil_net": network,
            "extralog": ["event"],
        }

    def test_stats_page_and_humanizers_render(self) -> None:
        app = create_app(self.collector)
        response = app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"GunCAD Mirror test-ref", response.data)
        self.assertIn(b"Sleeping", response.data)

        with app.app_context():
            humanize_bytes = app.jinja_env.filters["humanize_bytes"]
            humanize_seconds = app.jinja_env.filters["humanize_seconds"]
            self.assertEqual(humanize_bytes(1024), "1.0 KiB")
            self.assertEqual(humanize_bytes(1024**9), "1024.0 YiB")
            self.assertEqual(humanize_seconds(60), "1.0 minutes")
            self.assertEqual(humanize_seconds(60 * 60 * 24 * 7 * 52), "1.0 years")

    @patch("guncadmirror.webui.Thread")
    @patch("guncadmirror.webui.serve")
    def test_start_launches_waitress_daemon_thread(
        self, serve: Mock, thread_class: Mock
    ) -> None:
        thread = thread_class.return_value
        self.assertIs(start(self.collector), thread)
        kwargs = thread_class.call_args.kwargs
        self.assertEqual(kwargs["name"], "mirror-webui")
        self.assertTrue(kwargs["daemon"])
        self.assertIs(kwargs["target"], serve)
        self.assertEqual(kwargs["kwargs"]["port"], 5000)
        thread.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
