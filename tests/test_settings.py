from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from guncadmirror.settings import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_defaults_and_derived_paths(self):
        settings = Settings.from_env({})

        self.assertEqual(settings.data_dir, Path("/data"))
        self.assertIsNone(settings.max_releases_per_run)
        self.assertFalse(settings.enable_webui)
        self.assertTrue(settings.odysee_fallback)
        self.assertEqual(settings.lbry_concurrency, 4)
        self.assertEqual(settings.odysee_concurrency, 2)
        self.assertEqual(settings.finalize_concurrency, 2)
        self.assertEqual(
            settings.odysee_proxy_url,
            "https://api.na-backend.odysee.com/api/v1/proxy",
        )
        self.assertEqual(settings.state_path, Path("/data/mirror-state.sqlite3"))
        self.assertEqual(settings.outbox_dir, Path("/data/outbox"))
        self.assertEqual(settings.releases_dir, Path("/data/releases"))
        self.assertFalse(settings.publish_enabled)
        self.assertEqual(settings.publish_url, "")
        self.assertEqual(settings.publish_token, "")
        self.assertEqual(settings.publish_concurrency, 2)
        self.assertEqual(settings.publish_timeout, 60)

    def test_reads_every_supported_environment_shape(self):
        settings = Settings.from_env(
            {
                "MIRROR_API_ENDPOINT": "https://example.test/api/",
                "MIRROR_DATA_DIR": "/archive",
                "MIRROR_LBRY_URL": "http://lbry:5279",
                "MIRROR_ODYSEE_FALLBACK": "disabled",
                "MIRROR_ODYSEE_PROXY_URL": "https://fallback.example/proxy",
                "MIRROR_LBRY_CONCURRENCY": "10",
                "MIRROR_ODYSEE_CONCURRENCY": "3",
                "MIRROR_FINALIZE_CONCURRENCY": "4",
                "MIRROR_API_MAX_PAGES": "2",
                "MIRROR_MAX_RELEASES_PER_RUN": "3",
                "MIRROR_RELEASE_MAX_SIZE": "4",
                "MIRROR_MIN_FREE_SPACE": "5",
                "MIRROR_LOOP_INTERVAL": "6.5",
                "MIRROR_CYCLE_ERROR_INTERVAL": "6.75",
                "MIRROR_LBRY_STARTUP_TIMEOUT": "7",
                "MIRROR_DOWNLOAD_TIMEOUT": "8",
                "MIRROR_DOWNLOAD_POLL_INTERVAL": ".5",
                "MIRROR_RETRY_ATTEMPTS": "9",
                "MIRROR_RETRY_BACKOFF": "0",
                "MIRROR_ENABLE_WEBUI": "enabled",
                "MIRROR_BLACKLISTED_HANDLES": "bad, worse\nworst",
                "MIRROR_TORRENT_PIECE_LENGTH": "16384",
                "MIRROR_TORRENT_TRACKERS": (
                    "udp://tracker.test:80,http://tracker2.test/announce"
                ),
                "MIRROR_PUBLISH_ENABLED": "true",
                "MIRROR_PUBLISH_URL": "https://index.example/api/v2/torrents/publish/",
                "MIRROR_PUBLISH_TOKEN": "secret-token",
                "MIRROR_PUBLISH_CONCURRENCY": "3",
                "MIRROR_PUBLISH_TIMEOUT": "17.5",
            }
        )

        self.assertEqual(settings.data_dir, Path("/archive"))
        self.assertEqual(settings.api_max_pages, 2)
        self.assertFalse(settings.odysee_fallback)
        self.assertEqual(settings.odysee_proxy_url, "https://fallback.example/proxy")
        self.assertEqual(settings.lbry_concurrency, 10)
        self.assertEqual(settings.odysee_concurrency, 3)
        self.assertEqual(settings.finalize_concurrency, 4)
        self.assertEqual(settings.max_releases_per_run, 3)
        self.assertEqual(settings.max_release_size, 4)
        self.assertEqual(settings.min_free_space, 5)
        self.assertEqual(settings.loop_interval, 6.5)
        self.assertEqual(settings.cycle_error_interval, 6.75)
        self.assertEqual(settings.lbry_startup_timeout, 7)
        self.assertEqual(settings.download_timeout, 8)
        self.assertEqual(settings.download_poll_interval, 0.5)
        self.assertEqual(settings.retry_attempts, 9)
        self.assertEqual(settings.retry_backoff, 0)
        self.assertTrue(settings.enable_webui)
        self.assertEqual(settings.blacklisted_handles, ("bad", "worse", "worst"))
        self.assertEqual(len(settings.torrent_trackers), 2)
        self.assertTrue(settings.publish_enabled)
        self.assertEqual(
            settings.publish_url,
            "https://index.example/api/v2/torrents/publish/",
        )
        self.assertEqual(settings.publish_token, "secret-token")
        self.assertEqual(settings.publish_concurrency, 3)
        self.assertEqual(settings.publish_timeout, 17.5)

    def test_uses_process_environment_when_not_injected(self):
        with patch.dict("os.environ", {"MIRROR_ENABLE_WEBUI": "true"}, clear=True):
            self.assertTrue(Settings.from_env().enable_webui)

    def test_rejects_invalid_configuration(self):
        cases = [
            ({"MIRROR_ENABLE_WEBUI": "perhaps"}, "boolean"),
            ({"MIRROR_API_MAX_PAGES": "wat"}, "integer"),
            ({"MIRROR_API_MAX_PAGES": "0"}, "at least"),
            ({"MIRROR_LBRY_CONCURRENCY": "0"}, "at least"),
            ({"MIRROR_LOOP_INTERVAL": "wat"}, "numeric"),
            ({"MIRROR_LOOP_INTERVAL": "0"}, "at least"),
            ({"MIRROR_CYCLE_ERROR_INTERVAL": "0"}, "at least"),
            ({"MIRROR_API_ENDPOINT": "ftp://bad"}, "absolute HTTP"),
            ({"MIRROR_LBRY_URL": "http://user:pass@host"}, "credentials"),
            ({"MIRROR_ODYSEE_PROXY_URL": "not-a-url"}, "absolute HTTP"),
            ({"MIRROR_TORRENT_PIECE_LENGTH": "20000"}, "power of two"),
            ({"MIRROR_TORRENT_TRACKERS": "wat://tracker"}, "tracker URL"),
            ({"MIRROR_PUBLISH_ENABLED": "true"}, "MIRROR_PUBLISH_URL"),
            (
                {
                    "MIRROR_PUBLISH_ENABLED": "true",
                    "MIRROR_PUBLISH_URL": "https://index.example/publish/",
                },
                "MIRROR_PUBLISH_TOKEN",
            ),
            ({"MIRROR_PUBLISH_TOKEN": "bad token"}, "non-whitespace"),
            ({"MIRROR_PUBLISH_CONCURRENCY": "0"}, "at least"),
            ({"MIRROR_PUBLISH_TIMEOUT": "0"}, "at least"),
        ]
        for env, message in cases:
            with (
                self.subTest(env=env),
                self.assertRaisesRegex(ConfigurationError, message),
            ):
                Settings.from_env(env)
