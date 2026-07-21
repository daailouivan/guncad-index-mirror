from __future__ import annotations

import base64
import configparser
import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def load_entrypoint():
    path = Path(__file__).parents[1] / "contrib" / "qbittorrent-entrypoint.py"
    spec = importlib.util.spec_from_file_location("qbittorrent_entrypoint", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise RuntimeError("cannot load qBittorrent entrypoint")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QBitTorrentEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_entrypoint()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.module.CONFIG_PATH = (
            Path(self.temporary.name)
            / "config"
            / "qBittorrent"
            / "config"
            / "qBittorrent.conf"
        )

    def test_configures_private_seed_client_and_preserves_session_settings(
        self,
    ) -> None:
        path = self.module.CONFIG_PATH
        path.parent.mkdir(parents=True)
        path.write_text("[BitTorrent]\nSession\\ResumeDataStorageType=SQLite\n")
        environment = {
            "QBITTORRENT_USERNAME": "mirror",
            "QBITTORRENT_PASSWORD": "secret",
            "QBITTORRENT_HOST_HEADER_VALIDATION": "false",
            "QBT_WEBUI_PORT": "8080",
            "QBT_TORRENTING_PORT": "6881",
        }

        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(self.module.os, "urandom", return_value=b"s" * 16),
        ):
            self.module.configure()

        config = configparser.RawConfigParser(interpolation=None)
        config.optionxform = str
        config.read(path)
        self.assertEqual(
            config["BitTorrent"][r"Session\ResumeDataStorageType"], "SQLite"
        )
        self.assertEqual(config["BitTorrent"][r"Session\DefaultSavePath"], "/downloads")
        self.assertEqual(
            config["BitTorrent"][r"Session\QueueingSystemEnabled"], "false"
        )
        self.assertEqual(config["Preferences"][r"WebUI\Username"], "mirror")
        self.assertEqual(config["Preferences"][r"WebUI\HostHeaderValidation"], "false")
        self.assertEqual(config["Preferences"][r"WebUI\CSRFProtection"], "true")
        expected_digest = hashlib.pbkdf2_hmac(
            "sha512", b"secret", b"s" * 16, self.module.PASSWORD_ITERATIONS
        )
        expected = (
            '"@ByteArray('
            + base64.b64encode(b"s" * 16).decode()
            + ":"
            + base64.b64encode(expected_digest).decode()
            + ')"'
        )
        self.assertEqual(config["Preferences"][r"WebUI\Password_PBKDF2"], expected)
        self.assertNotIn("secret", path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rejects_missing_credentials_invalid_boolean_and_ports(self) -> None:
        cases = (
            ({}, "QBITTORRENT_USERNAME must be set"),
            (
                {
                    "QBITTORRENT_USERNAME": "mirror",
                    "QBITTORRENT_PASSWORD": "secret",
                    "QBITTORRENT_HOST_HEADER_VALIDATION": "perhaps",
                },
                "must be a boolean",
            ),
            (
                {
                    "QBITTORRENT_USERNAME": "mirror",
                    "QBITTORRENT_PASSWORD": "secret",
                    "QBT_TORRENTING_PORT": "0",
                },
                "ports must be between",
            ),
            (
                {
                    "QBITTORRENT_USERNAME": "mirror",
                    "QBITTORRENT_PASSWORD": "secret",
                    "QBT_WEBUI_PORT": "eight-thousand",
                },
                "ports must be integers",
            ),
        )
        for environment, message in cases:
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, environment, clear=True),
                self.assertRaisesRegex(SystemExit, message),
            ):
                self.module.configure()


if __name__ == "__main__":
    unittest.main()
