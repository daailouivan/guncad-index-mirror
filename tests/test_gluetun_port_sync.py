from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class GluetunPortSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.capture = self.directory / "wget-arguments"
        self.fake_bin = self.directory / "bin"
        self.fake_bin.mkdir()
        wget = self.fake_bin / "wget"
        wget.write_text('#! /bin/sh\nprintf "%s\\n" "$@" >"$CAPTURE"\n')
        wget.chmod(0o755)
        self.script = (
            Path(__file__).parents[1] / "contrib" / "gluetun-qbittorrent-port.sh"
        )
        self.environment = {
            **os.environ,
            "CAPTURE": str(self.capture),
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
        }

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (self.script, *arguments),
            check=False,
            capture_output=True,
            env=self.environment,
            text=True,
        )

    def test_up_sets_forwarded_port_and_vpn_interface(self) -> None:
        result = self.run_script("up", "49152", "tun7")

        self.assertEqual(result.returncode, 0)
        arguments = self.capture.read_text().splitlines()
        self.assertIn(
            '--post-data=json={"listen_port":49152,'
            '"current_network_interface":"tun7","random_port":false,"upnp":false}',
            arguments,
        )
        self.assertEqual(
            arguments[-1],
            "http://127.0.0.1:8080/api/v2/app/setPreferences",
        )

    def test_down_disables_torrent_listener(self) -> None:
        result = self.run_script("down")

        self.assertEqual(result.returncode, 0)
        self.assertIn(
            '--post-data=json={"listen_port":0,"current_network_interface":"lo",'
            '"random_port":false,"upnp":false}',
            self.capture.read_text().splitlines(),
        )

    def test_rejects_invalid_action_and_port(self) -> None:
        for arguments in ((), ("restart",), ("up", "nope"), ("up", "65536")):
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.capture.exists())


if __name__ == "__main__":
    unittest.main()
