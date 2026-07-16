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
            "mirror_api_endpoint": "https://index.example/api/v2/releases/",
            "mirror_api_max_pages": 2,
            "mirror_max_releases_per_run": None,
            "mirror_lbry_url": "http://127.0.0.1:5279",
            "mirror_lbry_concurrency": 4,
            "mirror_odysee_concurrency": 2,
            "mirror_finalize_concurrency": 2,
            "mirror_enable_webui": True,
            "mirror_blacklisted_handles": (),
            "mirror_release_max_size": 1024,
            "mirror_min_free_space": 512,
            "mirror_loop_interval": 3600,
            "mirror_cycle_error_interval": 60,
            "mirror_download_timeout": 600,
            "mirror_torrent_piece_length": 1024**2,
            "mirror_torrent_trackers": (),
            "mirror_data_dir": "/data",
            "mirror_releases_dir": "/data/releases",
            "mirror_outbox_dir": "/data/outbox",
            "disk_space_used": 123,
            "job_counts": {"awaiting_index": 2, "excluded": 1},
            "known_jobs": 3,
            "activity": None,
            "activities": [],
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
        self.assertIn(b"A well regulated Militia", response.data)
        self.assertIn(b"shall not be infringed", response.data)
        self.assertIn(b"Sleeping", response.data)
        self.assertIn(b"Torrents staged", response.data)
        self.assertIn(b"excluded by policy", response.data)
        self.assertIn(b"Publication stops at the local outbox", response.data)
        self.assertNotIn(b"LBRY-only mode", response.data)
        self.assertNotIn(b"Assemble Files", response.data)

        with app.app_context():
            humanize_bytes = app.jinja_env.filters["humanize_bytes"]
            humanize_seconds = app.jinja_env.filters["humanize_seconds"]
            self.assertEqual(humanize_bytes(1024), "1.0 KiB")
            self.assertEqual(humanize_bytes(1024**9), "1024.0 YiB")
            self.assertEqual(humanize_seconds(60), "1.0 minutes")
            self.assertEqual(humanize_seconds(60 * 60 * 24 * 7 * 52), "1.0 years")

    def test_active_release_progress_and_lbry_blob_states_render(self) -> None:
        activity = {
            "release_name": "GATALOG",
            "channel_handle": "@Prints.and.the.Revolution:c",
            "release_id": "a" * 40,
            "sd_hash": "b" * 96,
            "phase": "Acquiring from Odysee CDN",
            "transport": "odysee-cdn",
            "completed_bytes": 512,
            "total_bytes": 1024,
            "bytes_per_second": 128,
            "blobs_remaining": None,
        }
        self.collector.snapshot.return_value["activity"] = activity
        self.collector.snapshot.return_value["activities"] = [activity]
        app = create_app(self.collector)

        response = app.test_client().get("/")
        self.assertIn(b"GATALOG", response.data)
        self.assertIn(b"Acquiring from Odysee CDN", response.data)
        self.assertIn(b"50.0%", response.data)
        self.assertIn(b"128.0 B/s", response.data)
        self.assertIn(b"4.0 seconds remaining", response.data)
        self.assertIn(b"1 active job", response.data)

        activity.update(
            {
                "phase": "Acquiring from LBRY",
                "completed_bytes": None,
                "bytes_per_second": None,
                "blobs_remaining": 13,
            }
        )
        response = app.test_client().get("/")
        self.assertIn(b"13 LBRY blobs remaining", response.data)

        activity["blobs_remaining"] = None
        response = app.test_client().get("/")
        self.assertIn(b"Advertised size: 1.0 KiB", response.data)

        second = dict(activity, release_name="Second release", release_id="c" * 40)
        self.collector.snapshot.return_value["activities"] = [activity, second]
        response = app.test_client().get("/")
        self.assertIn(b"2 active jobs", response.data)
        self.assertIn(b"Second release", response.data)

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
