from __future__ import annotations

import tempfile
import unittest
from collections import deque
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import requests

from guncadmirror.cancellation import AcquisitionCancelled
from guncadmirror.lbry import (
    LbryAcquirer,
    LbryClient,
    LbryError,
    LbryMethodUnavailable,
    LbryProtocolError,
    LbryStreamUnavailable,
    LbryTimeout,
)

from .helpers import FakeResponse, QueueSession, make_release


class LbryClientTests(unittest.TestCase):
    def test_call_posts_json_rpc_and_returns_result(self) -> None:
        session = QueueSession(FakeResponse({"result": {"ok": True}}))
        client = LbryClient(
            "http://lbry:5279",
            attempts=1,
            backoff=0,
            session=session,
        )
        self.assertEqual(client.call("status", {"x": 1}), {"ok": True})
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "post")
        self.assertEqual(url, "http://lbry:5279")
        self.assertEqual(kwargs["json"], {"method": "status", "params": {"x": 1}})
        self.assertEqual(kwargs["timeout"], (5, 60))
        client.close()
        self.assertTrue(session.closed)

    def test_call_retries_transport_and_rpc_errors_with_backoff(self) -> None:
        sleeps: list[float] = []
        session = QueueSession(
            requests.ConnectionError("down"),
            FakeResponse({"error": {"message": "warming up"}}),
            FakeResponse({"result": "ready"}),
        )
        client = LbryClient(
            "http://lbry:5279",
            attempts=3,
            backoff=2,
            session=session,
            sleep=sleeps.append,
        )
        with self.assertLogs("guncad-mirror.lbry", level="WARNING"):
            self.assertEqual(client.call("status"), "ready")
        self.assertEqual(sleeps, [2, 4])

    def test_call_rejects_malformed_and_nested_error_responses(self) -> None:
        cases = [
            FakeResponse(json_error=requests.exceptions.JSONDecodeError("bad", "x", 0)),
            FakeResponse([]),
            FakeResponse({"result": {"error": "broken"}}),
        ]
        for response in cases:
            with self.subTest(response=response):
                client = LbryClient(
                    "http://lbry:5279",
                    attempts=1,
                    backoff=0,
                    session=QueueSession(response),
                )
                with self.assertRaises(LbryError):
                    client.call("status")

    def test_call_exhaustion_reports_attempt_count(self) -> None:
        client = LbryClient(
            "http://lbry:5279",
            attempts=2,
            backoff=0,
            session=QueueSession(
                requests.ConnectionError("nope"), requests.ConnectionError("still nope")
            ),
            sleep=lambda _: None,
        )
        with self.assertLogs("guncad-mirror.lbry", level="WARNING"):
            with self.assertRaisesRegex(LbryError, "after 2 attempts"):
                client.call("status")

        one_shot = LbryClient(
            "http://lbry:5279",
            attempts=5,
            backoff=0,
            session=QueueSession(requests.ConnectionError("nope")),
        )
        with self.assertRaisesRegex(LbryError, "after 1 attempts"):
            one_shot.call("status", attempts=1)
        with self.assertRaisesRegex(ValueError, "positive"):
            one_shot.call("status", attempts=0)

    def test_optional_method_absence_is_not_retried(self) -> None:
        session = QueueSession(
            FakeResponse(
                {"error": {"code": -32601, "message": "Command does not exist"}}
            )
        )
        client = LbryClient(
            "http://lbry:5279",
            attempts=5,
            backoff=1,
            session=session,
            sleep=Mock(),
        )
        with self.assertRaises(LbryMethodUnavailable):
            client.call("stream_get")
        self.assertEqual(len(session.calls), 1)

    def test_call_observes_stop_before_after_and_during_retry(self) -> None:
        stop = Event()
        stop.set()
        session = QueueSession()
        client = LbryClient(
            "http://lbry:5279",
            attempts=2,
            backoff=60,
            session=session,
        )
        with self.assertRaises(AcquisitionCancelled):
            client.call("status", stop=stop)
        self.assertEqual(session.calls, [])

        stop.clear()

        class CancellingSession(QueueSession):
            def post(self, url: str, **kwargs: object):
                stop.set()
                return super().post(url, **kwargs)

        client.session = CancellingSession(FakeResponse({"result": "ready"}))
        with self.assertRaises(AcquisitionCancelled):
            client.call("status", stop=stop)

        stop.clear()
        client.session = CancellingSession(requests.ConnectionError("offline"))
        with (
            self.assertLogs("guncad-mirror.lbry", level="WARNING"),
            self.assertRaises(AcquisitionCancelled),
        ):
            client.call("status", stop=stop)

    def test_ready_wait_is_resumably_cancellable(self) -> None:
        stop = Event()
        client = LbryClient("http://lbry:5279", attempts=1, backoff=0)

        def status(*_args: object, **_kwargs: object) -> object:
            stop.set()
            return {"is_running": False}

        client.call = Mock(side_effect=status)
        with self.assertRaises(AcquisitionCancelled):
            client.wait_until_ready(10, stop=stop)

    def test_wait_until_ready_requires_direct_stream_components(self) -> None:
        sleeps: list[float] = []
        client = LbryClient(
            "http://lbry:5279", attempts=1, backoff=0, sleep=sleeps.append
        )
        client.call = Mock(
            side_effect=[
                LbryError("booting"),
                {
                    "is_running": True,
                    "startup_status": {
                        "database": True,
                        "blob_manager": True,
                        "stream_manager": True,
                    },
                },
                {
                    "is_running": False,
                    "startup_status": {
                        "database": True,
                        "blob_manager": True,
                        "file_manager": True,
                    },
                },
            ]
        )
        ticks = iter([0, 0, 1, 2])
        client.wait_until_ready(10, poll_interval=0.5, monotonic=lambda: next(ticks))
        self.assertEqual(sleeps, [0.5, 0.5])
        self.assertEqual(client.call.call_count, 3)

    def test_wait_until_ready_times_out(self) -> None:
        sleeps: list[float] = []
        client = LbryClient(
            "http://lbry:5279", attempts=1, backoff=0, sleep=sleeps.append
        )
        client.call = Mock(return_value={"is_running": False})
        ticks = iter([0, 0, 2])
        with self.assertRaisesRegex(LbryTimeout, "not ready"):
            client.wait_until_ready(
                1, poll_interval=0.25, monotonic=lambda: next(ticks)
            )
        self.assertEqual(sleeps, [0.25])

    def test_file_lookup_validates_cardinality_and_shape(self) -> None:
        client = LbryClient("http://lbry:5279", attempts=1, backoff=0)
        client.call = Mock(return_value={"items": [{"sd_hash": "x"}]})
        self.assertEqual(client.file_for_sd_hash("x"), {"sd_hash": "x"})
        client.call.assert_called_with("file_list", {"sd_hash": "x"}, stop=None)

        for result in [None, {}, {"items": "bad"}, {"items": [1]}, {"items": [{}, {}]}]:
            with self.subTest(result=result):
                client.call = Mock(return_value=result)
                with self.assertRaises(LbryProtocolError):
                    client.file_for_sd_hash("x")
        client.call = Mock(return_value={"items": []})
        self.assertIsNone(client.file_for_sd_hash("x"))


class FakeLbryClient:
    def __init__(
        self, entries: list[object], calls: list[tuple[str, object]] | None = None
    ):
        self.entries = deque(entries)
        self.calls = calls if calls is not None else []

    def file_for_sd_hash(self, sd_hash: str, **_kwargs: object) -> object:
        self.calls.append(("file_list", sd_hash))
        return self.entries.popleft()

    def call(self, method: str, params: object, **kwargs: object) -> object:
        self.calls.append((method, params))
        if method == "get":
            return {"sd_hash": params["expected"]} if "expected" in params else None
        return True


class LbryAcquirerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.release = make_release()

    def _entry(self, path: Path, **overrides: object) -> dict[str, object]:
        entry: dict[str, object] = {
            "sd_hash": self.release.sd_hash,
            "status": "finished",
            "blobs_remaining": 0,
            "download_path": str(path),
        }
        entry.update(overrides)
        return entry

    def _acquirer(self, client: object, **overrides: object) -> LbryAcquirer:
        return LbryAcquirer(
            client,
            data_root=self.root,
            download_timeout=overrides.get("download_timeout", 10),
            poll_interval=1,
            sleep=overrides.get("sleep", lambda _: None),
            monotonic=overrides.get("monotonic", lambda: 0),
            progress=overrides.get("progress"),
        )

    def test_reuses_a_complete_verified_local_stream(self) -> None:
        payload = self.root / "existing.zip"
        payload.write_bytes(b"payload")
        client = FakeLbryClient([self._entry(payload)])
        result = self._acquirer(client).acquire(self.release, self.root / "release")
        self.assertEqual(result, payload)
        self.assertEqual(client.calls, [("file_list", self.release.sd_hash)])

    def test_reports_release_identity_and_remaining_blobs(self) -> None:
        payload = self.root / "existing.zip"
        payload.write_bytes(b"payload")
        progress = Mock()
        client = FakeLbryClient([self._entry(payload)])

        self._acquirer(client, progress=progress).acquire(
            self.release,
            self.root / "release",
        )

        updates = [call.args[0] for call in progress.update_activity.call_args_list]
        self.assertEqual(updates[-1].release, self.release)
        self.assertEqual(updates[-1].phase.value, "Acquiring from LBRY")
        self.assertEqual(updates[-1].transport, "lbry")
        self.assertEqual(updates[-1].blobs_remaining, 0)

    def test_resumes_known_stream_and_waits_for_truthful_completion(self) -> None:
        payload = self.root / "release" / "payload.zip"
        payload.parent.mkdir()
        payload.write_bytes(b"payload")
        client = FakeLbryClient(
            [
                {"sd_hash": self.release.sd_hash, "status": "running"},
                {"sd_hash": self.release.sd_hash, "status": "running"},
                self._entry(payload),
            ]
        )
        result = self._acquirer(client).acquire(self.release, payload.parent)
        self.assertEqual(result, payload)
        self.assertEqual(
            [call[0] for call in client.calls],
            ["file_list", "file_save", "file_list", "file_list"],
        )

    def test_new_stream_uses_direct_sd_rpc_then_waits_for_completion(self) -> None:
        payload = self.root / "release" / "payload.zip"
        payload.parent.mkdir()
        payload.write_bytes(b"payload")
        client = Mock()
        client.file_for_sd_hash.side_effect = [None, self._entry(payload)]
        client.call.return_value = {"sd_hash": self.release.sd_hash}
        result = self._acquirer(client).acquire(self.release, payload.parent)
        self.assertEqual(result, payload)
        method, params = client.call.call_args.args
        self.assertEqual(method, "stream_get")
        self.assertEqual(params["sd_hash"], self.release.sd_hash)
        self.assertTrue(params["save_file"])
        self.assertEqual(client.call.call_args.kwargs["read_timeout"], 40)

    def test_stock_daemon_falls_back_to_claim_resolution(self) -> None:
        payload = self.root / "release" / "payload.zip"
        payload.parent.mkdir()
        payload.write_bytes(b"payload")
        client = Mock()
        client.file_for_sd_hash.side_effect = [None, self._entry(payload)]
        client.call.side_effect = [
            LbryMethodUnavailable("missing"),
            {"sd_hash": self.release.sd_hash},
        ]
        with self.assertLogs("guncad-mirror.acquire", level="WARNING"):
            result = self._acquirer(client).acquire(self.release, payload.parent)
        self.assertEqual(result, payload)
        self.assertEqual(
            [call.args[0] for call in client.call.call_args_list],
            ["stream_get", "get"],
        )
        self.assertEqual(
            client.call.call_args_list[1].args[1]["uri"], self.release.url_lbry
        )

    def test_rejects_claim_drift_and_false_resume_results(self) -> None:
        client = Mock()
        client.file_for_sd_hash.return_value = None
        client.call.return_value = {"sd_hash": "0" * 96}
        with self.assertRaisesRegex(LbryProtocolError, "expected"):
            self._acquirer(client).acquire(self.release, self.root / "release")

        for response in [None, False]:
            with self.subTest(response=response):
                client = Mock()
                client.file_for_sd_hash.return_value = {"sd_hash": self.release.sd_hash}
                client.call.return_value = response
                with self.assertRaises(LbryError):
                    self._acquirer(client).acquire(self.release, self.root / "release")

    def test_restarts_stopped_stream_once_then_reports_unavailable(self) -> None:
        stopped = {
            "sd_hash": self.release.sd_hash,
            "status": "stopped",
            "stopped": True,
            "blobs_remaining": 3,
        }
        client = Mock()
        client.file_for_sd_hash.side_effect = [stopped, stopped]
        client.call.return_value = True
        ticks = iter([0, 0])
        with self.assertRaisesRegex(LbryStreamUnavailable, "3 blobs remaining"):
            self._acquirer(
                client, download_timeout=2, monotonic=lambda: next(ticks)
            ).acquire(self.release, self.root / "release")
        client.call.assert_called_once()
        self.assertEqual(client.call.call_args.args[0], "file_save")

    def test_stopped_stream_reports_unknown_remaining_count(self) -> None:
        stopped = {
            "sd_hash": self.release.sd_hash,
            "status": "stopped",
            "stopped": True,
        }
        client = Mock()
        client.file_for_sd_hash.side_effect = [None, stopped, stopped]
        client.call.side_effect = [
            {"sd_hash": self.release.sd_hash},
            True,
        ]
        ticks = iter([0, 0, 1])
        with self.assertLogs("guncad-mirror.acquire", level="WARNING"):
            with self.assertRaisesRegex(LbryStreamUnavailable, "unknown blob count"):
                self._acquirer(
                    client, download_timeout=2, monotonic=lambda: next(ticks)
                ).acquire(self.release, self.root / "release")

    def test_running_stream_still_uses_stall_timeout_and_is_stopped(self) -> None:
        running = {
            "sd_hash": self.release.sd_hash,
            "status": "running",
            "stopped": False,
        }
        client = Mock()
        client.file_for_sd_hash.side_effect = [running, running, running]
        client.call.return_value = True
        ticks = iter([0, 0, 1, 2])
        with self.assertRaises(LbryTimeout):
            self._acquirer(
                client, download_timeout=2, monotonic=lambda: next(ticks)
            ).acquire(self.release, self.root / "release")
        self.assertEqual(client.call.call_count, 2)
        method, params = client.call.call_args.args
        self.assertEqual(method, "file_set_status")
        self.assertEqual(
            params,
            {"status": "stop", "sd_hash": self.release.sd_hash},
        )
        self.assertEqual(client.call.call_args.kwargs["attempts"], 1)

    def test_active_blob_progress_renews_the_timeout(self) -> None:
        payload = self.root / "release" / "payload.zip"
        payload.parent.mkdir()
        payload.write_bytes(b"payload")
        running = {
            "sd_hash": self.release.sd_hash,
            "status": "running",
            "stopped": False,
        }
        client = Mock()
        client.file_for_sd_hash.side_effect = [
            None,
            {**running, "blobs_remaining": 3},
            {**running, "blobs_remaining": 2},
            self._entry(payload),
        ]
        client.call.return_value = {"sd_hash": self.release.sd_hash}
        ticks = iter([0, 0, 1, 2, 3])

        result = self._acquirer(
            client,
            download_timeout=2,
            monotonic=lambda: next(ticks),
        ).acquire(self.release, payload.parent)

        self.assertEqual(result, payload)
        self.assertEqual(client.file_for_sd_hash.call_count, 4)

    def test_completed_entry_must_be_safe_real_and_exact_size(self) -> None:
        valid = self.root / "valid.zip"
        valid.write_bytes(b"wrong")
        invalid_entries = [
            self._entry(valid, sd_hash="0" * 96),
            self._entry(valid, download_path=""),
            self._entry(self.root / "missing.zip"),
            self._entry(Path("/etc/passwd")),
            self._entry(valid),
        ]
        for entry in invalid_entries:
            with self.subTest(entry=entry):
                client = FakeLbryClient([entry])
                if entry.get("download_path") == "" or "missing" in str(
                    entry.get("download_path")
                ):
                    # Incomplete-looking entries proceed to resume and need a second poll.
                    client = Mock()
                    client.file_for_sd_hash.side_effect = [entry, entry]
                    client.call.return_value = False
                    with self.assertRaises(LbryError):
                        self._acquirer(client).acquire(
                            self.release, self.root / "release"
                        )
                else:
                    with self.assertRaises((LbryProtocolError, ValueError)):
                        self._acquirer(client).acquire(
                            self.release, self.root / "release"
                        )

    def test_completed_legacy_entry_accepts_unknown_nonzero_size(self) -> None:
        payload = self.root / "legacy.zip"
        payload.write_bytes(b"legacy")
        release = replace(self.release, size=None, sha384=None)
        client = FakeLbryClient([self._entry(payload)])

        self.assertEqual(
            self._acquirer(client).acquire(release, self.root / "release"), payload
        )

        payload.write_bytes(b"")
        client = FakeLbryClient([self._entry(payload)])
        with self.assertRaisesRegex(LbryProtocolError, "empty"):
            self._acquirer(client).acquire(release, self.root / "release")

    def test_acquisition_stop_preserves_the_sdk_stream_for_resume(self) -> None:
        stop = Event()
        stop.set()
        client = Mock()
        with self.assertRaises(AcquisitionCancelled):
            self._acquirer(client).acquire(
                self.release,
                self.root / "pre-stopped",
                stop=stop,
            )
        client.file_for_sd_hash.assert_not_called()

        stop.clear()
        running = {
            "sd_hash": self.release.sd_hash,
            "status": "running",
            "stopped": False,
            "blobs_remaining": 2,
        }
        calls = 0

        def lookup(*_args: object, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 2:
                stop.set()
            return running

        client.file_for_sd_hash.side_effect = lookup
        client.call.return_value = True
        with self.assertRaises(AcquisitionCancelled):
            self._acquirer(client).acquire(
                self.release,
                self.root / "running",
                stop=stop,
            )
        self.assertEqual(client.call.call_args.args[0], "file_save")
        self.assertNotIn(
            "file_set_status", [call.args[0] for call in client.call.mock_calls]
        )

    def test_long_sdk_operations_cap_their_socket_timeout(self) -> None:
        client = Mock()
        client.file_for_sd_hash.return_value = None
        client.call.return_value = {"sd_hash": self.release.sd_hash}
        ticks = iter([0, 3601])
        acquirer = self._acquirer(
            client,
            download_timeout=3600,
            monotonic=lambda: next(ticks),
        )

        with self.assertRaises(LbryTimeout):
            acquirer.acquire(self.release, self.root / "long")

        stream_call = client.call.call_args_list[0]
        self.assertEqual(stream_call.kwargs["read_timeout"], 60)


if __name__ == "__main__":
    unittest.main()
