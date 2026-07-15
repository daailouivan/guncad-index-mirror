from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import urlsplit

import requests

from .cancellation import check_cancelled, wait_or_cancel
from .index_client import USER_AGENT
from .models import Release
from .paths import ensure_within, safe_component

PLAYER_HOST = "player.odycdn.com"
CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
DOWNLOAD_CHUNK_SIZE = 1024**2
PROGRESS_INTERVAL = 1024**3


class OdyseeError(RuntimeError):
    """Odysee could not supply an independently verifiable fallback."""


class OdyseeProtocolError(OdyseeError):
    """Odysee returned data that did not match the Index release."""


class OdyseeUnavailable(OdyseeError):
    """A transient Odysee operation exhausted its retries."""


@dataclass(frozen=True, slots=True)
class OdyseeAcquisition:
    path: Path
    source_url: str


class OdyseeAcquirer:
    """Recover claimed plaintext from Odysee after LBRY peers fail."""

    def __init__(
        self,
        proxy_url: str,
        *,
        data_root: Path,
        attempts: int,
        backoff: float,
        read_timeout: float = 60,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ):
        self.proxy_url = proxy_url
        self.data_root = data_root
        self.attempts = attempts
        self.backoff = backoff
        self.read_timeout = read_timeout
        self.session = session or requests.Session()
        self.sleep = sleep
        self.logger = logger or logging.getLogger("guncad-mirror.odysee")

    def close(self) -> None:
        self.session.close()

    def acquire(
        self,
        release: Release,
        output_directory: Path,
        *,
        stop: Event | None = None,
    ) -> OdyseeAcquisition:
        check_cancelled(stop)
        if release.size is None or release.sha384 is None:
            raise OdyseeProtocolError(
                "Odysee fallback requires an independent Index size and SHA-384"
            )

        source, permanent_url = self._resolve_source(release, stop=stop)
        result = self._call(
            "get",
            {
                "uri": permanent_url,
                "save_file": False,
            },
            stop=stop,
        )
        if not isinstance(result, Mapping):
            raise OdyseeProtocolError("Odysee get result must be a JSON object")
        source_url = result.get("streaming_url")
        if not isinstance(source_url, str) or not source_url:
            raise OdyseeProtocolError("Odysee get result has no streaming URL")
        self._validate_stream_url(source_url, release)

        raw_name = source.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise OdyseeProtocolError("resolved Odysee source has no file name")
        file_name = safe_component(
            Path(raw_name.replace("\\", "/")).name,
            fallback=f"{release.id}.bin",
            max_length=160,
        )
        output_directory.mkdir(parents=True, exist_ok=True)
        output_path = ensure_within(self.data_root, output_directory / file_name)
        self._download(source_url, output_path, release, stop=stop)
        return OdyseeAcquisition(path=output_path, source_url=source_url)

    def _resolve_source(
        self, release: Release, *, stop: Event | None = None
    ) -> tuple[Mapping[str, Any], str]:
        result = self._call("resolve", {"urls": [release.url_lbry]}, stop=stop)
        if not isinstance(result, Mapping):
            raise OdyseeProtocolError("Odysee resolve result must be a JSON object")
        claim = result.get(release.url_lbry)
        if not isinstance(claim, Mapping):
            raise OdyseeProtocolError("Odysee did not resolve the requested LBRY URI")
        if claim.get("claim_id") != release.id:
            raise OdyseeProtocolError(
                f"Odysee resolved claim {claim.get('claim_id')!r}, expected {release.id}"
            )

        value = claim.get("value")
        source = value.get("source") if isinstance(value, Mapping) else None
        if not isinstance(source, Mapping):
            raise OdyseeProtocolError("resolved Odysee claim has no source object")
        expected = {
            "sd_hash": release.sd_hash,
            "hash": release.sha384,
            "size": str(release.size),
        }
        for field, expected_value in expected.items():
            if str(source.get(field)) != expected_value:
                raise OdyseeProtocolError(
                    f"resolved Odysee source {field} {source.get(field)!r}, "
                    f"expected {expected_value}"
                )

        permanent_url = claim.get("permanent_url")
        if (
            not isinstance(permanent_url, str)
            or not permanent_url.startswith("lbry://")
            or permanent_url.rsplit("#", 1)[-1] != release.id
        ):
            raise OdyseeProtocolError("resolved Odysee claim has no permanent URL")
        return source, permanent_url

    def _call(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        stop: Event | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            check_cancelled(stop)
            try:
                response = self.session.post(
                    self.proxy_url,
                    params={"m": method},
                    json={
                        "jsonrpc": "2.0",
                        "method": method,
                        "params": dict(params),
                        "id": attempt,
                    },
                    headers={
                        "Content-Type": "application/json-rpc",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=(5, self.read_timeout),
                )
                response.raise_for_status()
                try:
                    payload = response.json()
                except (TypeError, ValueError) as error:
                    raise OdyseeProtocolError(
                        f"Odysee {method} returned a non-JSON response"
                    ) from error
                if not isinstance(payload, Mapping):
                    raise OdyseeProtocolError(
                        f"Odysee {method} response must be a JSON object"
                    )
                error_value = payload.get("error")
                result = payload.get("result")
                if error_value is not None:
                    raise OdyseeUnavailable(f"Odysee {method}: {error_value}")
                if isinstance(result, Mapping) and result.get("error") is not None:
                    raise OdyseeUnavailable(f"Odysee {method}: {result.get('error')}")
                check_cancelled(stop)
                return result
            except OdyseeProtocolError:
                raise
            except (requests.RequestException, OdyseeUnavailable) as error:
                last_error = error
                if attempt == self.attempts:
                    break
                delay = self.backoff * (2 ** (attempt - 1))
                self.logger.warning(
                    "Odysee %s attempt %d/%d failed: %s; retrying in %.1fs",
                    method,
                    attempt,
                    self.attempts,
                    error,
                    delay,
                )
                wait_or_cancel(stop, delay, sleep=self.sleep)
        raise OdyseeUnavailable(
            f"Odysee {method} failed after {self.attempts} attempts: {last_error}"
        ) from last_error

    def _download(
        self,
        source_url: str,
        output_path: Path,
        release: Release,
        *,
        stop: Event | None = None,
    ) -> None:
        check_cancelled(stop)
        expected_size = release.size
        if expected_size is None:  # Narrowed by acquire(); keeps the invariant local.
            raise OdyseeProtocolError("Odysee fallback has no expected size")

        partial_path = output_path.with_name(f".{output_path.name}.odysee.part")
        if output_path.is_file():
            if output_path.stat().st_size == expected_size:
                return
            if partial_path.exists():
                output_path.unlink()
            else:
                output_path.replace(partial_path)
        if partial_path.exists() and partial_path.stat().st_size > expected_size:
            partial_path.unlink()

        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            check_cancelled(stop)
            start = partial_path.stat().st_size if partial_path.exists() else 0
            if start == expected_size:
                partial_path.replace(output_path)
                return
            try:
                self._download_range(
                    source_url,
                    partial_path,
                    release,
                    start=start,
                    stop=stop,
                )
                if partial_path.stat().st_size != expected_size:
                    raise OdyseeUnavailable(
                        f"Odysee CDN ended at {partial_path.stat().st_size} of "
                        f"{expected_size} bytes"
                    )
                partial_path.replace(output_path)
                _fsync_directory(output_path.parent)
                return
            except OdyseeProtocolError:
                partial_path.unlink(missing_ok=True)
                raise
            except (OSError, requests.RequestException, OdyseeUnavailable) as error:
                last_error = error
                if attempt == self.attempts:
                    break
                delay = self.backoff * (2 ** (attempt - 1))
                self.logger.warning(
                    "Odysee CDN attempt %d/%d stopped at %d/%d bytes: %s; "
                    "resuming in %.1fs",
                    attempt,
                    self.attempts,
                    partial_path.stat().st_size if partial_path.exists() else 0,
                    expected_size,
                    error,
                    delay,
                )
                wait_or_cancel(stop, delay, sleep=self.sleep)
        raise OdyseeUnavailable(
            f"Odysee CDN failed after {self.attempts} attempts: {last_error}"
        ) from last_error

    def _download_range(
        self,
        source_url: str,
        partial_path: Path,
        release: Release,
        *,
        start: int,
        stop: Event | None = None,
    ) -> None:
        check_cancelled(stop)
        expected_size = release.size
        if expected_size is None:
            raise OdyseeProtocolError("Odysee fallback has no expected size")
        headers = {
            "Accept-Encoding": "identity",
            "Origin": "https://odysee.com",
            "Range": f"bytes={start}-",
            "Referer": "https://odysee.com/",
            "User-Agent": USER_AGENT,
        }
        with self.session.get(
            source_url,
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=(5, self.read_timeout),
        ) as response:
            response.raise_for_status()
            check_cancelled(stop)
            if response.status_code != 206:
                raise OdyseeProtocolError(
                    f"Odysee CDN returned HTTP {response.status_code}, expected 206"
                )
            self._validate_stream_url(response.url, release)
            content_range = response.headers.get("Content-Range", "")
            match = CONTENT_RANGE_RE.fullmatch(content_range)
            if match is None:
                raise OdyseeProtocolError(
                    f"Odysee CDN returned invalid Content-Range {content_range!r}"
                )
            range_start, range_end, total = map(int, match.groups())
            if (
                range_start != start
                or range_end != expected_size - 1
                or total != expected_size
            ):
                raise OdyseeProtocolError(
                    f"Odysee CDN returned range {content_range!r}, expected "
                    f"bytes {start}-{expected_size - 1}/{expected_size}"
                )

            written = start
            next_progress = ((written // PROGRESS_INTERVAL) + 1) * PROGRESS_INTERVAL
            with partial_path.open("ab") as output:
                try:
                    for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        if not chunk:
                            continue
                        check_cancelled(stop)
                        written += len(chunk)
                        if written > expected_size:
                            raise OdyseeProtocolError(
                                f"Odysee CDN exceeded expected size {expected_size}"
                            )
                        output.write(chunk)
                        if written >= next_progress:
                            self.logger.info(
                                "Odysee fallback for %s reached %d/%d bytes",
                                release.name,
                                written,
                                expected_size,
                            )
                            next_progress += PROGRESS_INTERVAL
                finally:
                    output.flush()
                    os.fsync(output.fileno())

    @staticmethod
    def _validate_stream_url(source_url: str, release: Release) -> None:
        parsed = urlsplit(source_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != PLAYER_HOST
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.fragment
        ):
            raise OdyseeProtocolError(
                f"Odysee returned an untrusted stream URL: {source_url}"
            )
        parts = tuple(part for part in parsed.path.split("/") if part)
        if (
            release.id not in parts
            or not parts
            or not parts[-1].startswith(f"{release.sd_hash[:6]}.")
        ):
            raise OdyseeProtocolError(
                "Odysee stream URL does not identify the expected claim and descriptor"
            )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
