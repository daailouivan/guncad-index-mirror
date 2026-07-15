from __future__ import annotations

import tempfile
import unittest
from collections import deque
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

import requests

from guncadmirror.cancellation import AcquisitionCancelled
from guncadmirror.odysee import (
    OdyseeAcquirer,
    OdyseeProtocolError,
    OdyseeUnavailable,
)

from .helpers import make_release


class FakeResponse:
    def __init__(
        self,
        *,
        payload: Any = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        url: str = "https://player.odycdn.com/",
        chunks: list[bytes | Exception] | None = None,
        error: Exception | None = None,
    ):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.chunks = chunks or []
        self.error = error

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def iter_content(self, *, chunk_size: int):
        self.chunk_size = chunk_size
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class FakeSession:
    def __init__(
        self,
        *,
        posts: list[FakeResponse | Exception],
        gets: list[FakeResponse | Exception],
    ):
        self.posts = deque(posts)
        self.gets = deque(gets)
        self.post_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append((url, kwargs))
        response = self.posts.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append((url, kwargs))
        response = self.gets.popleft()
        if isinstance(response, Exception):
            raise response
        return response


class OdyseeAcquirerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.content = b"payload"
        self.release = make_release(self.content)
        self.stream_url = (
            "https://player.odycdn.com/v6/streams/"
            f"{self.release.id}/{self.release.sd_hash[:6]}.mp4"
        )

    def _resolve_response(
        self,
        *,
        claim_overrides: dict[str, Any] | None = None,
        **source_overrides: Any,
    ) -> FakeResponse:
        source = {
            "hash": self.release.sha384,
            "name": "folder\\payload.zip",
            "sd_hash": self.release.sd_hash,
            "size": str(self.release.size),
            **source_overrides,
        }
        claim = {
            "claim_id": self.release.id,
            "permanent_url": f"lbry://release#{self.release.id}",
            "value": {"source": source},
            **(claim_overrides or {}),
        }
        return FakeResponse(payload={"result": {self.release.url_lbry: claim}})

    def _get_response(self, url: str | None = None) -> FakeResponse:
        return FakeResponse(
            payload={"result": {"streaming_url": url or self.stream_url}}
        )

    def _stream_response(
        self,
        start: int,
        chunks: list[bytes | Exception],
        *,
        url: str | None = None,
    ) -> FakeResponse:
        return FakeResponse(
            status_code=206,
            headers={
                "Content-Range": (
                    f"bytes {start}-{len(self.content) - 1}/{len(self.content)}"
                )
            },
            url=url or self.stream_url,
            chunks=chunks,
        )

    def _acquirer(
        self, session: FakeSession, *, attempts: int = 2, sleep=lambda _delay: None
    ) -> OdyseeAcquirer:
        return OdyseeAcquirer(
            "https://api.example/proxy",
            data_root=self.root,
            attempts=attempts,
            backoff=1,
            session=session,
            sleep=sleep,
        )

    def test_validates_claim_and_downloads_exact_ranged_plaintext(self) -> None:
        session = FakeSession(
            posts=[self._resolve_response(), self._get_response()],
            gets=[self._stream_response(0, [b"pay", b"", b"load"])],
        )
        acquirer = self._acquirer(session)

        result = acquirer.acquire(self.release, self.root / "release")

        self.assertEqual(result.path.read_bytes(), self.content)
        self.assertEqual(result.path.name, "payload.zip")
        self.assertEqual(result.source_url, self.stream_url)
        self.assertEqual(session.get_calls[0][1]["headers"]["Range"], "bytes=0-")
        self.assertEqual(
            session.get_calls[0][1]["headers"]["Origin"], "https://odysee.com"
        )
        self.assertFalse((result.path.parent / ".payload.zip.odysee.part").exists())
        acquirer.close()
        self.assertTrue(session.closed)

    def test_resumes_partial_file_after_transient_disconnect(self) -> None:
        partial = self.root / "release" / ".payload.zip.odysee.part"
        partial.parent.mkdir()
        partial.write_bytes(b"pay")
        session = FakeSession(
            posts=[self._resolve_response(), self._get_response()],
            gets=[
                self._stream_response(
                    3,
                    [b"lo", requests.ConnectionError("reset")],
                ),
                self._stream_response(5, [b"ad"]),
            ],
        )
        delays: list[float] = []

        with self.assertLogs("guncad-mirror.odysee", level="WARNING"):
            result = self._acquirer(session, sleep=delays.append).acquire(
                self.release, partial.parent
            )

        self.assertEqual(result.path.read_bytes(), self.content)
        self.assertEqual(
            [call[1]["headers"]["Range"] for call in session.get_calls],
            ["bytes=3-", "bytes=5-"],
        )
        self.assertEqual(delays, [1])

    def test_retries_proxy_transport_but_rejects_non_json(self) -> None:
        session = FakeSession(
            posts=[
                requests.ConnectionError("down"),
                self._resolve_response(),
                self._get_response(),
            ],
            gets=[self._stream_response(0, [self.content])],
        )
        delays: list[float] = []
        with self.assertLogs("guncad-mirror.odysee", level="WARNING"):
            self._acquirer(session, sleep=delays.append).acquire(
                self.release, self.root / "release"
            )
        self.assertEqual(delays, [1])

        broken = FakeSession(
            posts=[FakeResponse(payload=ValueError("html"))],
            gets=[],
        )
        with self.assertRaisesRegex(OdyseeProtocolError, "non-JSON"):
            self._acquirer(broken, attempts=1).acquire(
                self.release, self.root / "other"
            )

    def test_rejects_malformed_or_drifting_proxy_results(self) -> None:
        cases = [
            ([FakeResponse(payload={"result": []})], "resolve result"),
            ([FakeResponse(payload={"result": {}})], "did not resolve"),
            (
                [self._resolve_response(claim_overrides={"claim_id": "c" * 40})],
                "resolved claim",
            ),
            (
                [self._resolve_response(claim_overrides={"value": {}})],
                "no source object",
            ),
            (
                [
                    self._resolve_response(
                        claim_overrides={"permanent_url": "lbry://release#wrong"}
                    )
                ],
                "no permanent URL",
            ),
            (
                [self._resolve_response(), FakeResponse(payload={"result": []})],
                "get result",
            ),
            (
                [self._resolve_response(), FakeResponse(payload={"result": {}})],
                "no streaming URL",
            ),
            (
                [self._resolve_response(name=""), self._get_response()],
                "no file name",
            ),
        ]

        for index, (posts, message) in enumerate(cases):
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(OdyseeProtocolError, message),
            ):
                self._acquirer(FakeSession(posts=posts, gets=[]), attempts=1).acquire(
                    self.release, self.root / f"malformed-{index}"
                )

    def test_proxy_errors_retry_then_exhaust_with_context(self) -> None:
        session = FakeSession(
            posts=[
                FakeResponse(payload={"error": "backend unavailable"}),
                FakeResponse(payload={"result": {"error": "wallet unavailable"}}),
            ],
            gets=[],
        )
        delays: list[float] = []

        with (
            self.assertLogs("guncad-mirror.odysee", level="WARNING"),
            self.assertRaisesRegex(OdyseeUnavailable, "failed after 2.*wallet"),
        ):
            self._acquirer(session, sleep=delays.append).acquire(
                self.release, self.root / "proxy-errors"
            )

        self.assertEqual(delays, [1])
        self.assertEqual([call[1]["json"]["id"] for call in session.post_calls], [1, 2])

    def test_proxy_response_must_be_an_object(self) -> None:
        session = FakeSession(posts=[FakeResponse(payload=[])], gets=[])

        with self.assertRaisesRegex(OdyseeProtocolError, "response must"):
            self._acquirer(session, attempts=1).acquire(
                self.release, self.root / "proxy-list"
            )

    def test_fails_closed_on_identity_url_and_range_contradictions(self) -> None:
        cases = [
            (
                [self._resolve_response(hash="0" * 96)],
                [],
                "source hash",
            ),
            (
                [
                    self._resolve_response(),
                    self._get_response("https://evil.example/file.zip"),
                ],
                [],
                "untrusted stream URL",
            ),
            (
                [self._resolve_response(), self._get_response()],
                [
                    FakeResponse(
                        status_code=206,
                        headers={"Content-Range": "wat"},
                        url=self.stream_url,
                        chunks=[self.content],
                    )
                ],
                "invalid Content-Range",
            ),
            (
                [self._resolve_response(), self._get_response()],
                [
                    FakeResponse(
                        status_code=200,
                        headers={"Content-Range": "bytes 0-6/7"},
                        url=self.stream_url,
                        chunks=[self.content],
                    )
                ],
                "expected 206",
            ),
            (
                [self._resolve_response(), self._get_response()],
                [
                    FakeResponse(
                        status_code=206,
                        headers={"Content-Range": "bytes 1-6/7"},
                        url=self.stream_url,
                        chunks=[self.content],
                    )
                ],
                "returned range",
            ),
            (
                [self._resolve_response(), self._get_response()],
                [self._stream_response(0, [self.content + b"!"])],
                "exceeded expected size",
            ),
            (
                [self._resolve_response(), self._get_response()],
                [
                    self._stream_response(
                        0,
                        [self.content],
                        url=(
                            "https://player.odycdn.com/v6/streams/"
                            f"{'c' * 40}/{self.release.sd_hash[:6]}.mp4"
                        ),
                    )
                ],
                "expected claim and descriptor",
            ),
        ]
        for posts, gets, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(OdyseeProtocolError, message),
            ):
                self._acquirer(FakeSession(posts=posts, gets=gets), attempts=1).acquire(
                    self.release, self.root / message.replace(" ", "-")
                )

    def test_reuses_and_normalizes_local_resume_states(self) -> None:
        cases = [
            ("complete-output", self.content, None, None, 0),
            ("short-output", b"pay", None, b"load", 3),
            ("prefer-partial", b"wrong", b"pay", b"load", 3),
            ("oversized-partial", None, b"payload!", self.content, 0),
            ("complete-partial", None, self.content, None, 0),
        ]

        for name, output_content, partial_content, download, start in cases:
            with self.subTest(name=name):
                directory = self.root / name
                directory.mkdir()
                output = directory / "payload.zip"
                partial = directory / ".payload.zip.odysee.part"
                if output_content is not None:
                    output.write_bytes(output_content)
                if partial_content is not None:
                    partial.write_bytes(partial_content)
                gets = (
                    []
                    if download is None
                    else [self._stream_response(start, [download])]
                )
                session = FakeSession(
                    posts=[self._resolve_response(), self._get_response()],
                    gets=gets,
                )

                result = self._acquirer(session).acquire(self.release, directory)

                self.assertEqual(result.path.read_bytes(), self.content)
                self.assertFalse(partial.exists())
                self.assertEqual(len(session.get_calls), int(download is not None))
                if download is not None:
                    self.assertEqual(
                        session.get_calls[0][1]["headers"]["Range"],
                        f"bytes={start}-",
                    )

    def test_short_response_is_retried_from_observed_length(self) -> None:
        session = FakeSession(
            posts=[self._resolve_response(), self._get_response()],
            gets=[
                self._stream_response(0, [b"pay"]),
                self._stream_response(3, [b"load"]),
            ],
        )
        delays: list[float] = []

        with self.assertLogs("guncad-mirror.odysee", level="WARNING"):
            result = self._acquirer(session, sleep=delays.append).acquire(
                self.release, self.root / "short-response"
            )

        self.assertEqual(result.path.read_bytes(), self.content)
        self.assertEqual(
            [call[1]["headers"]["Range"] for call in session.get_calls],
            ["bytes=0-", "bytes=3-"],
        )
        self.assertEqual(delays, [1])

    def test_reports_large_download_progress(self) -> None:
        session = FakeSession(
            posts=[self._resolve_response(), self._get_response()],
            gets=[self._stream_response(0, [b"pay", b"load"])],
        )

        with (
            patch("guncadmirror.odysee.PROGRESS_INTERVAL", 3),
            self.assertLogs("guncad-mirror.odysee", level="INFO") as logs,
        ):
            self._acquirer(session).acquire(self.release, self.root / "progress")

        self.assertTrue(any("reached 3/7 bytes" in line for line in logs.output))

    def test_download_helpers_defend_the_size_invariant(self) -> None:
        release = replace(self.release, size=None)
        acquirer = self._acquirer(FakeSession(posts=[], gets=[]))

        with self.assertRaisesRegex(OdyseeProtocolError, "no expected size"):
            acquirer._download(self.stream_url, self.root / "payload.zip", release)
        with self.assertRaisesRegex(OdyseeProtocolError, "no expected size"):
            acquirer._download_range(
                self.stream_url,
                self.root / ".payload.zip.odysee.part",
                release,
                start=0,
            )

    def test_rejects_uncorroborated_release_and_exhausted_cdn(self) -> None:
        legacy = make_release(self.content)
        object.__setattr__(legacy, "size", None)
        object.__setattr__(legacy, "sha384", None)
        session = FakeSession(posts=[], gets=[])
        with self.assertRaisesRegex(OdyseeProtocolError, "independent"):
            self._acquirer(session).acquire(legacy, self.root / "legacy")

        session = FakeSession(
            posts=[self._resolve_response(), self._get_response()],
            gets=[
                requests.ConnectionError("down"),
                requests.ConnectionError("still down"),
            ],
        )
        with self.assertRaisesRegex(OdyseeUnavailable, "failed after 2"):
            self._acquirer(session).acquire(self.release, self.root / "failed")

    def test_stop_event_preserves_a_fsynced_ranged_partial(self) -> None:
        stop = Event()
        stop.set()
        session = FakeSession(posts=[], gets=[])
        with self.assertRaises(AcquisitionCancelled):
            self._acquirer(session).acquire(
                self.release,
                self.root / "pre-stopped",
                stop=stop,
            )
        self.assertEqual(session.post_calls, [])

        stop.clear()
        response = self._stream_response(0, [])

        def chunks(*, chunk_size: int):
            self.assertEqual(chunk_size, 1024**2)
            yield b"pay"
            stop.set()
            yield b"load"

        response.iter_content = chunks
        session = FakeSession(
            posts=[self._resolve_response(), self._get_response()],
            gets=[response],
        )
        directory = self.root / "cancelled"
        with self.assertRaises(AcquisitionCancelled):
            self._acquirer(session).acquire(
                self.release,
                directory,
                stop=stop,
            )

        partial = directory / ".payload.zip.odysee.part"
        self.assertEqual(partial.read_bytes(), b"pay")
        self.assertFalse((directory / "payload.zip").exists())

    def test_stop_event_interrupts_proxy_retry_backoff(self) -> None:
        stop = Event()

        class CancellingSession(FakeSession):
            def post(self, url: str, **kwargs: Any) -> FakeResponse:
                stop.set()
                return super().post(url, **kwargs)

        session = CancellingSession(
            posts=[requests.ConnectionError("offline")],
            gets=[],
        )
        with (
            self.assertLogs("guncad-mirror.odysee", level="WARNING"),
            self.assertRaises(AcquisitionCancelled),
        ):
            self._acquirer(session).acquire(
                self.release,
                self.root / "retry-stop",
                stop=stop,
            )


if __name__ == "__main__":
    unittest.main()
