from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from guncadmirror.qbittorrent import (
    QBitArtifactError,
    QBitClient,
    QBitConfigurationError,
    QBitRetryableError,
)


class QBitClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = Mock()
        self.client = QBitClient(
            "http://qbittorrent:8080/",
            timeout=13,
            username="mirror",
            password="secret",
            session=self.session,
        )
        self.session.post.return_value = Mock(status_code=204)

    def response(
        self,
        status: int = 200,
        document: object | None = None,
        *,
        text: str = "ok",
    ) -> Mock:
        response = Mock(status_code=status, text=text)
        response.json.return_value = document
        self.session.request.return_value = response
        return response

    def torrent_document(self, **overrides: object) -> dict[str, object]:
        return {
            "hash": "a" * 40,
            "content_path": "/downloads/releases/payload.zip",
            "save_path": "/downloads/releases",
            "progress": 1.0,
            "amount_left": 0,
            "state": "stalledUP",
            "force_start": True,
        } | overrides

    def test_logs_in_once_and_reports_versions(self) -> None:
        self.session.request.side_effect = (
            Mock(status_code=200, text="v5.2.3"),
            Mock(status_code=200, text="2.14.3"),
        )
        self.assertEqual(self.client.versions(), ("v5.2.3", "2.14.3"))
        self.assertEqual(self.session.post.call_count, 1)
        _, login = self.session.post.call_args
        self.assertEqual(login["data"], {"username": "mirror", "password": "secret"})
        self.assertEqual(login["headers"]["Origin"], "http://qbittorrent:8080")
        _, request = self.session.request.call_args
        self.assertEqual(request["timeout"], (5, 13))

    def test_api_key_is_stateless_and_skips_login(self) -> None:
        self.client = QBitClient(
            "https://qbit.example",
            timeout=3,
            api_key="qbt_" + "x" * 28,
            session=self.session,
        )
        self.response(document={"connection_status": "connected", "dht_nodes": 4})
        self.assertEqual(self.client.transfer().dht_nodes, 4)
        self.session.post.assert_not_called()
        _, request = self.session.request.call_args
        self.assertEqual(
            request["headers"]["Authorization"],
            "Bearer qbt_" + "x" * 28,
        )

    def test_adds_completed_torrent_in_seed_mode_and_controls_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            torrent = Path(temporary) / "artifact.torrent"
            torrent.write_bytes(b"torrent")
            self.response(status=200)
            self.client.add(
                torrent,
                save_path="/downloads/releases",
                category="guncad-mirror",
                tag="guncad-mirror",
            )
        _, request = self.session.request.call_args
        self.assertEqual(request["data"]["skip_checking"], "true")
        self.assertEqual(request["data"]["paused"], "false")
        self.assertEqual(request["data"]["autoTMM"], "false")
        self.assertEqual(request["data"]["ratioLimit"], "-1")
        self.assertEqual(request["files"]["torrents"][1], b"torrent")

        self.client.force_start("a" * 40)
        args, request = self.session.request.call_args
        self.assertTrue(args[1].endswith("/api/v2/torrents/setForceStart"))
        self.assertEqual(request["data"]["value"], "true")

        self.client.reannounce("a" * 40)
        args, _ = self.session.request.call_args
        self.assertTrue(args[1].endswith("/api/v2/torrents/reannounce"))

    def test_observation_requires_complete_upload_state_and_discovery(self) -> None:
        self.response(document=[self.torrent_document()])
        torrent = self.client.torrent("a" * 40)
        self.assertTrue(torrent.upload_capable)

        self.response(document={"connection_status": "firewalled", "dht_nodes": 1})
        transfer = self.client.transfer()
        self.assertEqual(transfer.connection_status, "firewalled")

        self.response(document=[{"status": 0}, {"status": 2}, {"status": 4}])
        self.assertEqual(self.client.working_trackers("a" * 40), 1)

        responses = iter(
            (
                [self.torrent_document()],
                {"connection_status": "connected", "dht_nodes": 0},
                [{"status": 2}],
            )
        )
        self.session.request.side_effect = lambda *_args, **_kwargs: Mock(
            status_code=200,
            json=lambda: next(responses),
        )
        observation = self.client.observe("a" * 40)
        self.assertTrue(observation.green)

    def test_missing_torrent_and_non_green_states_are_reported(self) -> None:
        self.response(document=[])
        self.assertIsNone(self.client.torrent("a" * 40))

        for overrides in (
            {"progress": 0.9},
            {"amount_left": 1},
            {"state": "checkingUP"},
        ):
            with self.subTest(overrides=overrides):
                self.response(document=[self.torrent_document(**overrides)])
                self.assertFalse(self.client.torrent("a" * 40).upload_capable)

    def test_network_server_auth_and_rejected_torrent_failures_are_classified(
        self,
    ) -> None:
        self.session.post.side_effect = requests.ConnectionError("refused")
        with self.assertRaises(QBitRetryableError) as raised:
            self.client.transfer()
        self.assertEqual(raised.exception.code, "network_error")

        self.session.post.side_effect = None
        self.session.post.return_value = Mock(status_code=401)
        self.client._authenticated = False
        with self.assertRaises(QBitConfigurationError) as raised:
            self.client.transfer()
        self.assertEqual(raised.exception.code, "authentication_failed")

        self.session.post.return_value = Mock(status_code=204)
        self.client._authenticated = False
        self.response(status=503)
        with self.assertRaises(QBitRetryableError) as raised:
            self.client.transfer()
        self.assertEqual(raised.exception.code, "http_503")

        self.response(status=415)
        with tempfile.TemporaryDirectory() as temporary:
            torrent = Path(temporary) / "bad.torrent"
            torrent.write_bytes(b"bad")
            with self.assertRaises(QBitArtifactError):
                self.client.add(
                    torrent,
                    save_path="/downloads",
                    category="mirror",
                    tag="mirror",
                )

    def test_invalid_responses_and_values_are_rejected(self) -> None:
        cases = (
            ("torrent", {"document": {}}, "a" * 40),
            (
                "torrent",
                {"document": [self.torrent_document(progress=float("nan"))]},
                "a" * 40,
            ),
            ("torrent", {"document": [self.torrent_document(hash="b" * 40)]}, "a" * 40),
            (
                "transfer",
                {"document": {"connection_status": "wat", "dht_nodes": 0}},
                None,
            ),
            ("working_trackers", {"document": [{"status": "two"}]}, "a" * 40),
        )
        for method, response, argument in cases:
            with self.subTest(method=method, response=response):
                self.response(**response)
                with self.assertRaises(QBitConfigurationError):
                    if argument is None:
                        getattr(self.client, method)()
                    else:
                        getattr(self.client, method)(argument)

        response = self.response(document={})
        response.json.side_effect = ValueError("no JSON")
        with self.assertRaises(QBitConfigurationError):
            self.client.transfer()

        self.response(text="not-a-version")
        with self.assertRaises(QBitConfigurationError):
            self.client.versions()

    def test_unreadable_torrent_and_close_are_exposed(self) -> None:
        with self.assertRaises(QBitArtifactError):
            self.client.add(
                Path("/does/not/exist.torrent"),
                save_path="/downloads",
                category="mirror",
                tag="mirror",
            )
        self.client.close()
        self.session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
