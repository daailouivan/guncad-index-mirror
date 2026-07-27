from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from guncadmirror.state import JobStore
from guncadmirror.tracker_policy import (
    MAX_POLICY_BYTES,
    TrackerPolicyClient,
    TrackerPolicyError,
    TrackerPolicyManager,
    TrackerPolicyResponse,
    TrackerState,
    parse_tracker_policy,
)


def policy_bytes(
    trackers: list[dict[str, object]] | None = None,
    *,
    strip: bool = False,
) -> bytes:
    return json.dumps(
        {
            "strip_uploader_trackers": strip,
            "trackers": trackers or [],
        },
        separators=(",", ":"),
    ).encode()


def policy_response(
    *,
    status: int = 200,
    raw: bytes | None = None,
    etag: str = '"policy-v1"',
    headers: dict[str, str] | None = None,
) -> Mock:
    document = policy_bytes() if raw is None else raw
    response_headers = {"ETag": etag, "Content-Length": str(len(document))}
    response_headers.update(headers or {})
    return Mock(
        status_code=status,
        headers=response_headers,
        content=document,
    )


class TrackerPolicyParserTests(unittest.TestCase):
    def test_parses_sorts_and_projects_tracker_states(self) -> None:
        raw = policy_bytes(
            [
                {
                    "url": "udp://disabled.example:80/announce",
                    "state": "disabled",
                    "position": 20,
                },
                {
                    "url": "https://enabled.example/announce",
                    "state": "enabled",
                    "position": 10,
                },
                {
                    "url": "udp://blacklisted.example:80/announce",
                    "state": "blacklisted",
                    "position": 10,
                },
            ],
            strip=True,
        )

        policy = parse_tracker_policy(raw)

        self.assertTrue(policy.strip_uploader_trackers)
        self.assertEqual(
            [tracker.state for tracker in policy.trackers],
            [
                TrackerState.ENABLED,
                TrackerState.BLACKLISTED,
                TrackerState.DISABLED,
            ],
        )
        self.assertEqual(policy.enabled, ("https://enabled.example/announce",))
        self.assertEqual(
            policy.blacklisted,
            frozenset({"udp://blacklisted.example:80/announce"}),
        )

    def test_rejects_malformed_policy_documents_and_entries(self) -> None:
        valid = {
            "url": "https://tracker.example/announce",
            "state": "enabled",
            "position": 1,
        }
        cases: list[bytes] = [
            b"",
            b"{",
            b"[]",
            json.dumps({"trackers": []}).encode(),
            json.dumps(
                {
                    "strip_uploader_trackers": False,
                    "trackers": [],
                    "surprise": True,
                }
            ).encode(),
            json.dumps({"strip_uploader_trackers": 0, "trackers": []}).encode(),
            policy_bytes([{"url": "https://tracker.example"}]),
            policy_bytes([valid | {"surprise": True}]),
            policy_bytes([valid | {"url": "ftp://tracker.example"}]),
            policy_bytes([valid | {"url": "https://user:pass@tracker.example"}]),
            policy_bytes([valid | {"state": "missing"}]),
            policy_bytes([valid | {"position": True}]),
            policy_bytes([valid | {"position": -1}]),
            policy_bytes([valid, valid]),
            b"x" * (MAX_POLICY_BYTES + 1),
        ]
        for raw in cases:
            with self.subTest(raw=raw[:100]), self.assertRaises(TrackerPolicyError):
                parse_tracker_policy(raw)


class TrackerPolicyClientTests(unittest.TestCase):
    endpoint = "https://index.example/api/v2/torrents/tracker-policy/"

    def setUp(self) -> None:
        self.session = Mock()
        self.client = TrackerPolicyClient(
            self.endpoint,
            timeout=12,
            session=self.session,
        )

    def test_fetches_conditionally_and_accepts_not_modified(self) -> None:
        raw = policy_bytes(
            [
                {
                    "url": "udp://tracker.example:80/announce",
                    "state": "enabled",
                    "position": 1,
                }
            ]
        )
        self.session.get.return_value = policy_response(raw=raw)

        response = self.client.fetch('"old"')

        self.assertEqual(response.document, raw)
        self.assertEqual(response.etag, '"policy-v1"')
        self.assertEqual(
            response.policy.enabled, ("udp://tracker.example:80/announce",)
        )
        self.session.get.assert_called_once_with(
            self.endpoint,
            headers={
                "Accept": "application/json",
                "User-Agent": unittest.mock.ANY,
                "If-None-Match": '"old"',
            },
            timeout=(5, 12),
        )

        self.session.get.return_value = policy_response(status=304)
        self.assertIsNone(self.client.fetch('"policy-v1"'))
        self.client.close()
        self.session.close.assert_called_once_with()

    def test_classifies_transport_status_and_response_failures(self) -> None:
        cases = [
            (
                requests.ConnectionError("offline"),
                None,
                "network_error",
            ),
            (None, policy_response(status=503), "http_503"),
            (None, policy_response(status=304), "invalid_response"),
            (None, policy_response(etag=""), "invalid_response"),
            (
                None,
                policy_response(headers={"Content-Length": "invalid"}),
                "invalid_response",
            ),
            (
                None,
                policy_response(headers={"Content-Length": str(MAX_POLICY_BYTES + 1)}),
                "response_too_large",
            ),
            (
                None,
                policy_response(raw=b"x" * (MAX_POLICY_BYTES + 1)),
                "response_too_large",
            ),
        ]
        for raised, response, code in cases:
            with self.subTest(code=code):
                self.session.reset_mock()
                if raised is not None:
                    self.session.get.side_effect = raised
                else:
                    self.session.get.side_effect = None
                    self.session.get.return_value = response
                with self.assertRaises(TrackerPolicyError) as caught:
                    self.client.fetch()
                self.assertEqual(caught.exception.code, code)


class TrackerPolicyManagerTests(unittest.TestCase):
    endpoint = "https://index.example/api/v2/torrents/tracker-policy/"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.now = 100.0
        self.store = JobStore(
            Path(self.temporary.name) / "state.sqlite3",
            clock=lambda: self.now,
        )
        self.events: list[str] = []

    def client(self) -> Mock:
        client = Mock()
        client.url = self.endpoint
        return client

    def test_persists_last_known_good_and_never_exposes_blacklisted_operator_hint(
        self,
    ) -> None:
        raw = policy_bytes(
            [
                {
                    "url": "https://index.example/announce",
                    "state": "enabled",
                    "position": 1,
                },
                {
                    "url": "udp://blocked.example:80/announce",
                    "state": "blacklisted",
                    "position": 2,
                },
            ]
        )
        client = self.client()
        client.fetch.return_value = TrackerPolicyResponse(
            policy=parse_tracker_policy(raw),
            etag='"v1"',
            document=raw,
        )
        manager = TrackerPolicyManager(
            self.store,
            client,
            operator_trackers=(
                "udp://blocked.example:80/announce",
                "udp://operator.example:80/announce",
            ),
            record_event=self.events.append,
            clock=lambda: self.now,
        )
        self.assertFalse(manager.removals_authoritative)

        manager.refresh()

        self.assertTrue(manager.removals_authoritative)
        self.assertEqual(
            manager.desired_trackers,
            (
                "https://index.example/announce",
                "udp://operator.example:80/announce",
            ),
        )
        self.assertEqual(manager.status.source, "remote")
        self.assertEqual(manager.status.cached_at, 100)
        self.assertEqual(manager.status.last_success_at, 100)

        self.now = 200
        unavailable = self.client()
        unavailable.fetch.side_effect = TrackerPolicyError(
            "network_error",
            "Index is offline",
        )
        reopened = TrackerPolicyManager(
            JobStore(self.store.path, clock=lambda: self.now),
            unavailable,
            operator_trackers=("udp://operator.example:80/announce",),
            record_event=self.events.append,
            clock=lambda: self.now,
        )
        self.assertEqual(reopened.status.source, "cache")

        self.assertEqual(reopened.refresh().enabled, manager.policy.enabled)
        self.assertEqual(reopened.refresh().enabled, manager.policy.enabled)
        unavailable.fetch.assert_called_with('"v1"')
        self.assertEqual(reopened.status.error_code, "network_error")
        self.assertEqual(
            sum("TRACKER POLICY DEGRADED" in event for event in self.events),
            1,
        )

        unavailable.fetch.side_effect = None
        unavailable.fetch.return_value = None
        reopened.refresh()
        self.assertIsNone(reopened.status.error_code)
        self.assertIn("TRACKER POLICY RECOVERED", self.events)
        reopened.close()
        unavailable.close.assert_called_once_with()

    def test_cache_is_not_reused_for_a_different_endpoint(self) -> None:
        self.store.save_tracker_policy_cache(
            self.endpoint,
            '"v1"',
            policy_bytes(),
        )
        client = self.client()
        client.url = "https://other.example/api/v2/torrents/tracker-policy/"

        manager = TrackerPolicyManager(self.store, client)

        self.assertEqual(manager.status.source, "empty")
        self.assertIsNone(manager.status.etag)
        self.assertFalse(manager.removals_authoritative)

    def test_disabled_policy_still_uses_operator_trackers(self) -> None:
        manager = TrackerPolicyManager(
            self.store,
            None,
            operator_trackers=("udp://operator.example:80/announce",),
        )

        self.assertEqual(
            manager.desired_trackers,
            ("udp://operator.example:80/announce",),
        )
        self.assertFalse(manager.status.enabled)
        self.assertTrue(manager.removals_authoritative)
        manager.close()


if __name__ == "__main__":
    unittest.main()
