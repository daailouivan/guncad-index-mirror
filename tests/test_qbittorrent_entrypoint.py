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
        self.assertEqual(config["BitTorrent"][r"Session\Port"], "6881")
        self.assertEqual(config["Preferences"][r"WebUI\Username"], "mirror")
        self.assertEqual(config["Preferences"][r"WebUI\HostHeaderValidation"], "false")
        self.assertEqual(config["Preferences"][r"WebUI\LocalHostAuth"], "true")
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

    def test_uses_gluetun_forwarded_port_and_localhost_auth_setting(self) -> None:
        port_file = Path(self.temporary.name) / "forwarded_port"
        port_file.write_text("49152\n")
        environment = {
            "QBITTORRENT_USERNAME": "mirror",
            "QBITTORRENT_PASSWORD": "secret",
            "QBITTORRENT_LOCALHOST_AUTH": "false",
            "QBT_TORRENTING_PORT": "6881",
            "QBT_TORRENTING_PORT_FILE": str(port_file),
        }

        with patch.dict(os.environ, environment, clear=True):
            self.module.configure()
            self.assertEqual(os.environ["QBT_TORRENTING_PORT"], "49152")

        config = configparser.RawConfigParser(interpolation=None)
        config.optionxform = str
        config.read(self.module.CONFIG_PATH)
        self.assertEqual(config["BitTorrent"][r"Session\Port"], "49152")
        self.assertEqual(config["Preferences"][r"WebUI\LocalHostAuth"], "false")

    def test_missing_or_empty_gluetun_port_file_uses_configured_port(self) -> None:
        port_file = Path(self.temporary.name) / "forwarded_port"
        environment = {
            "QBITTORRENT_USERNAME": "mirror",
            "QBITTORRENT_PASSWORD": "secret",
            "QBT_TORRENTING_PORT": "6882",
            "QBT_TORRENTING_PORT_FILE": str(port_file),
        }

        for create_empty in (False, True):
            with self.subTest(create_empty=create_empty):
                if create_empty:
                    port_file.write_text("\n")
                elif port_file.exists():
                    port_file.unlink()
                with patch.dict(os.environ, environment, clear=True):
                    self.module.configure()

                config = configparser.RawConfigParser(interpolation=None)
                config.optionxform = str
                config.read(self.module.CONFIG_PATH)
                self.assertEqual(config["BitTorrent"][r"Session\Port"], "6882")

    def test_rejects_missing_credentials_invalid_boolean_and_ports(self) -> None:
        invalid_port_file = Path(self.temporary.name) / "forwarded_port"
        invalid_port_file.write_text("not-a-port")
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
                    "QBITTORRENT_LOCALHOST_AUTH": "perhaps",
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
                "Web UI port must be an integer",
            ),
            (
                {
                    "QBITTORRENT_USERNAME": "mirror",
                    "QBITTORRENT_PASSWORD": "secret",
                    "QBT_TORRENTING_PORT_FILE": str(invalid_port_file),
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
