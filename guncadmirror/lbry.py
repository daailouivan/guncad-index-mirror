from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Event
from typing import Any

import requests

from .cancellation import check_cancelled, wait_or_cancel
from .index_client import USER_AGENT
from .models import Release
from .paths import ensure_within


class LbryError(RuntimeError):
    """The LBRY daemon could not complete an operation."""


class LbryProtocolError(LbryError):
    """The daemon returned malformed or contradictory data."""


class LbryTimeout(LbryError):
    """A daemon component or stream did not finish before its deadline."""


class LbryMethodUnavailable(LbryError):
    """The connected daemon does not provide an optional Mirror RPC."""


class LbryStreamUnavailable(LbryError):
    """A stream stopped after the daemon exhausted its peer search."""


class LbryClient:
    def __init__(
        self,
        url: str,
        *,
        attempts: int,
        backoff: float,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ):
        self.url = url
        self.attempts = attempts
        self.backoff = backoff
        self.session = session or requests.Session()
        self.sleep = sleep
        self.logger = logger or logging.getLogger("guncad-mirror.lbry")

    def close(self) -> None:
        self.session.close()

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        read_timeout: float = 60,
        attempts: int | None = None,
        stop: Event | None = None,
    ) -> Any:
        operation_attempts = self.attempts if attempts is None else attempts
        if operation_attempts < 1:
            raise ValueError("attempts must be positive")
        last_error: Exception | None = None
        for attempt in range(1, operation_attempts + 1):
            check_cancelled(stop)
            try:
                response = self.session.post(
                    self.url,
                    json={"method": method, "params": dict(params or {})},
                    headers={"User-Agent": USER_AGENT},
                    timeout=(5, read_timeout),
                )
                response.raise_for_status()
                try:
                    payload = response.json()
                except requests.exceptions.JSONDecodeError as error:
                    raise LbryProtocolError(
                        f"{method} returned a non-JSON response"
                    ) from error
                if not isinstance(payload, Mapping):
                    raise LbryProtocolError(f"{method} response must be a JSON object")
                if payload.get("error") is not None:
                    error_value = payload["error"]
                    if _is_method_not_found(error_value):
                        raise LbryMethodUnavailable(
                            f"{method}: {_error_text(error_value)}"
                        )
                    raise LbryError(f"{method}: {_error_text(error_value)}")
                result = payload.get("result")
                if isinstance(result, Mapping) and result.get("error") is not None:
                    raise LbryError(f"{method}: {_error_text(result['error'])}")
                check_cancelled(stop)
                return result
            except LbryMethodUnavailable:
                raise
            except (requests.RequestException, LbryError) as error:
                last_error = error
                if attempt == operation_attempts:
                    break
                delay = self.backoff * (2 ** (attempt - 1))
                self.logger.warning(
                    "LBRY %s attempt %d/%d failed: %s; retrying in %.1fs",
                    method,
                    attempt,
                    operation_attempts,
                    error,
                    delay,
                )
                wait_or_cancel(stop, delay, sleep=self.sleep)
        raise LbryError(
            f"{method} failed after {operation_attempts} attempts: {last_error}"
        )

    def wait_until_ready(
        self,
        timeout: float,
        *,
        poll_interval: float = 1,
        monotonic: Callable[[], float] = time.monotonic,
        stop: Event | None = None,
    ) -> None:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            check_cancelled(stop)
            try:
                status = self.call(
                    "status",
                    read_timeout=min(timeout, 30),
                    stop=stop,
                )
            except LbryError as error:
                self.logger.info("Waiting for LBRY daemon: %s", error)
            else:
                if _is_ready(status):
                    return
            wait_or_cancel(stop, poll_interval, sleep=self.sleep)
        raise LbryTimeout(f"LBRY daemon was not ready after {timeout:.1f}s")

    def file_for_sd_hash(
        self,
        sd_hash: str,
        *,
        stop: Event | None = None,
    ) -> Mapping[str, Any] | None:
        result = self.call("file_list", {"sd_hash": sd_hash}, stop=stop)
        if not isinstance(result, Mapping):
            raise LbryProtocolError("file_list result must be a JSON object")
        items = result.get("items")
        if not isinstance(items, list):
            raise LbryProtocolError("file_list result has no items list")
        if not items:
            return None
        if len(items) != 1 or not isinstance(items[0], Mapping):
            raise LbryProtocolError(
                f"file_list returned {len(items)} entries for one sd_hash"
            )
        return items[0]


class LbryAcquirer:
    def __init__(
        self,
        client: LbryClient,
        *,
        data_root: Path,
        download_timeout: float,
        poll_interval: float,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ):
        self.client = client
        self.data_root = data_root
        self.download_timeout = download_timeout
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.monotonic = monotonic
        self.logger = logger or logging.getLogger("guncad-mirror.acquire")

    def acquire(
        self,
        release: Release,
        output_directory: Path,
        *,
        stop: Event | None = None,
    ) -> Path:
        check_cancelled(stop)
        output_directory.mkdir(parents=True, exist_ok=True)
        existing = self.client.file_for_sd_hash(release.sd_hash, stop=stop)
        path = self._completed_path(existing, release)
        if path is not None:
            return path

        if existing is not None:
            self.logger.info("Resuming locally known stream %s", release.sd_hash[:12])
            self._save_existing(release, output_directory, stop=stop)
        else:
            result = self._start_unknown_stream(release, output_directory, stop=stop)
            if not isinstance(result, Mapping):
                raise LbryProtocolError(
                    "stream acquisition result must be a JSON object"
                )
            actual_sd_hash = result.get("sd_hash")
            if actual_sd_hash != release.sd_hash:
                raise LbryProtocolError(
                    f"claim resolved to sd_hash {actual_sd_hash!r}, expected {release.sd_hash}"
                )

        deadline = self.monotonic() + self.download_timeout
        last_blobs_remaining = _blobs_remaining(existing)
        save_restarted = existing is not None
        while self.monotonic() < deadline:
            check_cancelled(stop)
            entry = self.client.file_for_sd_hash(release.sd_hash, stop=stop)
            path = self._completed_path(entry, release)
            if path is not None:
                return path
            check_cancelled(stop)
            blobs_remaining = _blobs_remaining(entry)
            if (
                blobs_remaining is not None
                and last_blobs_remaining is not None
                and blobs_remaining < last_blobs_remaining
            ):
                self.logger.debug(
                    "Stream %s advanced from %d to %d blobs remaining",
                    release.sd_hash[:12],
                    last_blobs_remaining,
                    blobs_remaining,
                )
                deadline = self.monotonic() + self.download_timeout
                last_blobs_remaining = blobs_remaining
            elif last_blobs_remaining is None and blobs_remaining is not None:
                last_blobs_remaining = blobs_remaining
            if entry is not None and entry.get("stopped") is True:
                if save_restarted:
                    raise LbryStreamUnavailable(
                        f"stream {release.sd_hash} stopped after a resume attempt "
                        f"with {_remaining_description(blobs_remaining)}"
                    )
                self.logger.warning(
                    "Stream %s stopped with %s; making one final resume attempt",
                    release.sd_hash[:12],
                    _remaining_description(blobs_remaining),
                )
                self._save_existing(release, output_directory, stop=stop)
                save_restarted = True
            wait_or_cancel(stop, self.poll_interval, sleep=self.sleep)
        self._stop_timed_out_stream(release)
        raise LbryTimeout(
            f"stream {release.sd_hash} made no blob progress for "
            f"{self.download_timeout:.1f}s"
        )

    def _start_unknown_stream(
        self,
        release: Release,
        output_directory: Path,
        *,
        stop: Event | None = None,
    ) -> Any:
        try:
            self.logger.info("Acquiring stream directly from %s", release.sd_hash[:12])
            return self.client.call(
                "stream_get",
                {
                    "sd_hash": release.sd_hash,
                    "download_directory": str(output_directory),
                    "save_file": True,
                    "timeout": int(self.download_timeout),
                },
                read_timeout=min(self.download_timeout + 30, 60),
                stop=stop,
            )
        except LbryMethodUnavailable:
            self.logger.warning(
                "LBRY daemon has no stream_get RPC; resolving the claim URI instead"
            )
            return self.client.call(
                "get",
                {
                    "uri": release.url_lbry,
                    "download_directory": str(output_directory),
                    "save_file": True,
                    "timeout": int(self.download_timeout),
                },
                read_timeout=min(self.download_timeout + 30, 60),
                stop=stop,
            )

    def _save_existing(
        self,
        release: Release,
        output_directory: Path,
        *,
        stop: Event | None = None,
    ) -> None:
        result = self.client.call(
            "file_save",
            {
                "sd_hash": release.sd_hash,
                "download_directory": str(output_directory),
            },
            read_timeout=min(self.download_timeout + 30, 60),
            stop=stop,
        )
        if result is False or result is None:
            raise LbryError(f"file_save could not resume {release.sd_hash}")

    def _stop_timed_out_stream(self, release: Release) -> None:
        try:
            self.client.call(
                "file_set_status",
                {"status": "stop", "sd_hash": release.sd_hash},
                read_timeout=30,
                attempts=1,
            )
        except LbryError as error:
            self.logger.warning(
                "Could not stop timed-out stream %s: %s",
                release.sd_hash[:12],
                error,
            )

    def _completed_path(
        self, entry: Mapping[str, Any] | None, release: Release
    ) -> Path | None:
        if entry is None:
            return None
        actual_sd_hash = entry.get("sd_hash")
        if actual_sd_hash != release.sd_hash:
            raise LbryProtocolError(
                f"file_list returned sd_hash {actual_sd_hash!r}, expected {release.sd_hash}"
            )
        if entry.get("status") != "finished" or entry.get("blobs_remaining") != 0:
            return None
        raw_path = entry.get("download_path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        path = ensure_within(self.data_root, Path(raw_path))
        if not path.is_file():
            return None
        actual_size = path.stat().st_size
        if release.size is not None and actual_size != release.size:
            raise LbryProtocolError(
                f"completed file has size {actual_size}, expected {release.size}"
            )
        if actual_size == 0:
            raise LbryProtocolError("completed file is empty")
        return path


def _is_ready(status: Any) -> bool:
    if not isinstance(status, Mapping):
        return False
    startup = status.get("startup_status")
    if not isinstance(startup, Mapping):
        return False
    return all(
        startup.get(component) is True
        for component in ("database", "blob_manager", "file_manager")
    )


def _blobs_remaining(entry: Mapping[str, Any] | None) -> int | None:
    if entry is None:
        return None
    value = entry.get("blobs_remaining")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _remaining_description(value: int | None) -> str:
    return (
        "an unknown blob count remaining"
        if value is None
        else f"{value} blobs remaining"
    )


def _error_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("message") or value)
    return str(value)


def _is_method_not_found(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("code") == -32601
