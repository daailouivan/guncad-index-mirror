from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import Mock

import requests

from guncadmirror.index_publisher import (
    RESPONSE_SCHEMA,
    IndexPublisherClient,
    PublicationPaused,
    PublicationState,
    PublicationSubmission,
    RetryablePublicationError,
    encode_manifest,
)


class IndexPublisherClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = Mock()
        self.client = IndexPublisherClient(
            "https://index.example/api/v2/torrents/publish/",
            "secret-token",
            timeout=17,
            session=self.session,
            wall_clock=lambda: datetime(2026, 7, 20, tzinfo=UTC).timestamp(),
        )
        self.submission = PublicationSubmission(
            release_id="a" * 40,
            sd_hash="b" * 96,
            sha384="c" * 96,
            btih="d" * 40,
            payload_name="payload.zip",
            manifest=b"{}",
            torrent=b"torrent",
        )

    def response(self, status: int, document: object) -> Mock:
        response = Mock(status_code=status, headers={})
        response.json.return_value = document
        self.session.post.return_value = response
        return response

    def document(
        self,
        *,
        outcome: str = "created",
        canonical_btih: str | None = None,
    ) -> dict[str, object]:
        canonical_btih = canonical_btih or self.submission.btih
        return {
            "schema": RESPONSE_SCHEMA,
            "outcome": outcome,
            "canonical": canonical_btih == self.submission.btih,
            "receipt": {
                "sd_hash": self.submission.sd_hash,
                "sha384": self.submission.sha384,
                "btih": self.submission.btih,
            },
            "canonical_artifact": {
                "sha384": self.submission.sha384,
                "btih": canonical_btih,
                "torrent_url": f"https://index.example/torrents/{canonical_btih}/",
                "magnet_uri": f"magnet:?xt=urn:btih:{canonical_btih}",
                "winning_release_id": self.submission.release_id,
            },
        }

    def error_document(
        self,
        code: str = "checksum_mismatch",
        *,
        canonical: object = None,
    ) -> dict[str, object]:
        return {
            "schema": RESPONSE_SCHEMA,
            "error": {"code": code, "message": "Nope", "fields": []},
            "canonical_artifact": canonical,
        }

    def test_posts_exact_authenticated_multipart_and_closes_pool(self) -> None:
        self.response(201, self.document())

        result = self.client.publish(self.submission)

        self.assertEqual(result.state, PublicationState.PUBLISHED)
        self.assertEqual(result.outcome, "created")
        self.assertTrue(result.canonical)
        self.assertEqual(result.artifact.btih, self.submission.btih)
        _, kwargs = self.session.post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-token")
        self.assertEqual(set(kwargs["files"]), {"manifest", "torrent"})
        self.assertEqual(kwargs["timeout"], (5, 17))
        self.client.close()
        self.session.close.assert_called_once_with()

    def test_accepts_created_promoted_idempotent_and_duplicate_contracts(self) -> None:
        for status, outcome, expected_state, canonical_btih in (
            (201, "created", PublicationState.PUBLISHED, self.submission.btih),
            (201, "promoted", PublicationState.PUBLISHED, self.submission.btih),
            (200, "idempotent", PublicationState.PUBLISHED, self.submission.btih),
            (409, "artifact_duplicate", PublicationState.DUPLICATE, "e" * 40),
        ):
            with self.subTest(status=status, outcome=outcome):
                self.response(
                    status,
                    self.document(outcome=outcome, canonical_btih=canonical_btih),
                )
                result = self.client.publish(self.submission)
                self.assertEqual(result.state, expected_state)
                self.assertEqual(result.outcome, outcome)
                self.assertEqual(result.canonical, canonical_btih == "d" * 40)

    def test_returns_terminal_rejections_and_conflicts(self) -> None:
        self.response(400, self.error_document())
        rejected = self.client.publish(self.submission)
        self.assertEqual(rejected.state, PublicationState.REJECTED)
        self.assertEqual(rejected.error_code, "checksum_mismatch")
        self.assertIsNone(rejected.artifact)

        canonical = self.document()["canonical_artifact"]
        self.response(
            409,
            self.error_document("sd_hash_conflict", canonical=canonical),
        )
        conflict = self.client.publish(self.submission)
        self.assertEqual(conflict.state, PublicationState.CONFLICT)
        self.assertEqual(conflict.error_code, "sd_hash_conflict")
        self.assertEqual(conflict.artifact.btih, self.submission.btih)

        self.response(413, self.error_document("request_too_large"))
        self.assertEqual(
            self.client.publish(self.submission).state,
            PublicationState.REJECTED,
        )

    def test_network_rate_limit_and_server_failures_are_retryable(self) -> None:
        self.session.post.side_effect = requests.ConnectionError("reset")
        with self.assertRaises(RetryablePublicationError) as raised:
            self.client.publish(self.submission)
        self.assertEqual(raised.exception.code, "network_error")

        self.session.post.side_effect = None
        response = self.response(429, {})
        response.headers = {"Retry-After": "12.5"}
        with self.assertRaises(RetryablePublicationError) as raised:
            self.client.publish(self.submission)
        self.assertEqual(raised.exception.retry_after, 12.5)

        response.headers = {"Retry-After": "Mon, 20 Jul 2026 00:01:00 GMT"}
        with self.assertRaises(RetryablePublicationError) as raised:
            self.client.publish(self.submission)
        self.assertEqual(raised.exception.retry_after, 60)

        for status in (500, 502):
            with self.subTest(status=status):
                self.response(status, {})
                with self.assertRaises(RetryablePublicationError):
                    self.client.publish(self.submission)

    def test_auth_configuration_route_and_unknown_status_pause_globally(self) -> None:
        for status in (401, 403, 404, 503, 418):
            with self.subTest(status=status):
                self.response(status, self.error_document())
                with self.assertRaises(PublicationPaused):
                    self.client.publish(self.submission)

    def test_rejects_malformed_terminal_responses(self) -> None:
        cases: list[tuple[int, object]] = [
            (200, []),
            (200, {"schema": "wrong"}),
            (200, {"schema": RESPONSE_SCHEMA, "outcome": "created"}),
            (201, dict(self.document(), outcome="idempotent")),
            (201, dict(self.document(), canonical="yes")),
            (
                201,
                dict(
                    self.document(),
                    receipt={"sd_hash": "f" * 96},
                ),
            ),
            (
                201,
                dict(
                    self.document(),
                    canonical_artifact=None,
                ),
            ),
            (
                400,
                {
                    "schema": RESPONSE_SCHEMA,
                    "error": {"code": "BAD", "message": "x", "fields": []},
                },
            ),
        ]
        for status, document in cases:
            with self.subTest(status=status, document=document):
                self.response(status, document)
                with self.assertRaises(PublicationPaused):
                    self.client.publish(self.submission)

        response = self.response(200, {})
        response.json.side_effect = requests.JSONDecodeError("bad", "x", 0)
        with self.assertRaises(PublicationPaused):
            self.client.publish(self.submission)

    def test_rejects_invalid_canonical_artifact_fields(self) -> None:
        valid = self.document()
        for field, value in (
            ("sha384", "bad"),
            ("btih", "bad"),
            ("winning_release_id", "bad"),
            ("torrent_url", "ftp://index.example/file"),
            ("torrent_url", "https://user@index.example/file"),
            ("magnet_uri", "magnet:?xt=urn:btih:" + "e" * 40),
        ):
            with self.subTest(field=field):
                artifact = dict(valid["canonical_artifact"])
                artifact[field] = value
                self.response(201, dict(valid, canonical_artifact=artifact))
                with self.assertRaises(PublicationPaused):
                    self.client.publish(self.submission)

    def test_invalid_retry_after_is_ignored_and_past_date_becomes_zero(self) -> None:
        response = self.response(429, {})
        response.headers = {"Retry-After": "not-a-date"}
        with self.assertRaises(RetryablePublicationError) as raised:
            self.client.publish(self.submission)
        self.assertIsNone(raised.exception.retry_after)

        response.headers = {"Retry-After": "Sun, 19 Jul 2026 00:00:00 GMT"}
        with self.assertRaises(RetryablePublicationError) as raised:
            self.client.publish(self.submission)
        self.assertEqual(raised.exception.retry_after, 0)

    def test_manifest_encoding_is_canonical_and_bounded(self) -> None:
        self.assertEqual(encode_manifest({"z": 1, "a": 2}), b'{"a":2,"z":1}')
        with self.assertRaises(ValueError):
            encode_manifest({"huge": "x" * (64 * 1024)})


if __name__ == "__main__":
    unittest.main()
