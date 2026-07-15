from __future__ import annotations

import unittest

import requests

from guncadmirror.index_client import USER_AGENT, IndexClient, IndexError

from .helpers import FakeResponse, QueueSession, release_payload


class IndexClientTests(unittest.TestCase):
    endpoint = "https://index.example/api/v2/releases/?query=narrow"

    def test_follows_bounded_same_origin_pagination_and_skips_bad_rows(self) -> None:
        first = release_payload(name="First")
        second = release_payload(release_id="c" * 40, sd_hash="d" * 96, name="Second")
        unsupported = release_payload()
        unsupported["id"] = "printables-1"
        unsupported["origin"] = {"platform": "printables"}
        session = QueueSession(
            FakeResponse(
                {
                    "results": [first, unsupported, {"id": "malformed"}],
                    "next": "/api/v2/releases/?offset=1",
                }
            ),
            FakeResponse({"results": [second], "next": None}),
        )
        client = IndexClient(
            self.endpoint, max_pages=2, max_releases=None, session=session
        )

        with self.assertLogs("guncad-mirror.index", level="DEBUG") as logs:
            releases = list(client.releases())

        self.assertEqual([release.name for release in releases], ["First", "Second"])
        self.assertTrue(any("origin: printables" in line for line in logs.output))
        self.assertTrue(any("malformed Index release" in line for line in logs.output))
        self.assertEqual(
            [call[1] for call in session.calls],
            [self.endpoint, "https://index.example/api/v2/releases/?offset=1"],
        )
        self.assertEqual(session.calls[0][2]["headers"]["User-Agent"], USER_AGENT)
        self.assertEqual(session.calls[0][2]["timeout"], (5, 60))
        client.close()
        self.assertTrue(session.closed)

    def test_release_cap_stops_without_fetching_another_page(self) -> None:
        session = QueueSession(
            FakeResponse(
                {
                    "results": [release_payload(), release_payload(name="Unused")],
                    "next": "/next",
                }
            )
        )
        client = IndexClient(
            self.endpoint, max_pages=10, max_releases=1, session=session
        )
        self.assertEqual(len(list(client.releases())), 1)
        self.assertEqual(len(session.calls), 1)

    def test_stops_cleanly_at_page_limit(self) -> None:
        session = QueueSession(
            FakeResponse({"results": [release_payload()], "next": "/next"})
        )
        client = IndexClient(
            self.endpoint, max_pages=1, max_releases=None, session=session
        )
        with self.assertLogs("guncad-mirror.index", level="WARNING") as logs:
            self.assertEqual(len(list(client.releases())), 1)
        self.assertIn("page limit", logs.output[0])

    def test_rejects_invalid_api_documents(self) -> None:
        invalid_documents = [
            [],
            {},
            {"results": "not-a-list"},
            {"results": [], "next": 42},
            {"results": [], "next": "https://evil.example/releases"},
        ]
        for document in invalid_documents:
            with self.subTest(document=document):
                client = IndexClient(
                    self.endpoint,
                    max_pages=1,
                    max_releases=None,
                    session=QueueSession(FakeResponse(document)),
                )
                with self.assertRaises(IndexError):
                    list(client.releases())

    def test_rejects_non_json_and_pagination_loops(self) -> None:
        json_error = requests.exceptions.JSONDecodeError("bad", "x", 0)
        client = IndexClient(
            self.endpoint,
            max_pages=1,
            max_releases=None,
            session=QueueSession(FakeResponse(json_error=json_error)),
        )
        with self.assertRaisesRegex(IndexError, "failed after 1 attempts"):
            list(client.releases())

        loop_session = QueueSession(
            FakeResponse({"results": [], "next": self.endpoint})
        )
        client = IndexClient(
            self.endpoint, max_pages=2, max_releases=None, session=loop_session
        )
        with self.assertRaisesRegex(IndexError, "pagination loop"):
            list(client.releases())

    def test_http_errors_are_not_disguised_as_empty_results(self) -> None:
        failure = requests.HTTPError("503")
        client = IndexClient(
            self.endpoint,
            max_pages=1,
            max_releases=None,
            session=QueueSession(FakeResponse(status_error=failure)),
        )
        with self.assertRaises(IndexError):
            list(client.releases())

    def test_retries_transient_page_failures_without_duplicate_yields(self) -> None:
        sleeps: list[float] = []
        session = QueueSession(
            requests.ConnectionError("offline"),
            FakeResponse({"results": [release_payload()], "next": None}),
        )
        client = IndexClient(
            self.endpoint,
            max_pages=1,
            max_releases=None,
            attempts=2,
            backoff=3,
            session=session,
            sleep=sleeps.append,
        )
        with self.assertLogs("guncad-mirror.index", level="WARNING"):
            self.assertEqual(len(list(client.releases())), 1)
        self.assertEqual(sleeps, [3])


if __name__ == "__main__":
    unittest.main()
