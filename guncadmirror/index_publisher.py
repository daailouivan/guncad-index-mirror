from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import requests

from .models import PublicationState
from .sessions import ThreadLocalSessionPool

REQUEST_SCHEMA = "guncad-mirror-publication-v1"
RESPONSE_SCHEMA = "guncad-index-torrent-publication-v1"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_TORRENT_BYTES = 4 * 1024 * 1024
SHA384_RE = re.compile(r"^[0-9a-f]{96}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BTIH_RE = re.compile(r"^[0-9a-f]{40}$")
CLAIM_ID_RE = re.compile(r"^[0-9a-f]{40}$")
ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class PublicationClientError(RuntimeError):
    """The Index publication request did not reach a terminal outcome."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RetryablePublicationError(PublicationClientError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after: float | None = None,
    ):
        super().__init__(code, message)
        self.retry_after = retry_after


class PublicationPaused(PublicationClientError):
    """Publication is globally misconfigured or the protocol is incompatible."""


@dataclass(frozen=True, slots=True)
class PublicationSubmission:
    release_id: str
    sd_hash: str
    sha384: str
    btih: str
    payload_name: str
    manifest: bytes
    torrent: bytes


@dataclass(frozen=True, slots=True)
class CanonicalArtifact:
    sha384: str
    btih: str
    torrent_url: str
    magnet_uri: str
    winning_release_id: str


@dataclass(frozen=True, slots=True)
class PublicationResult:
    state: PublicationState
    outcome: str | None
    canonical: bool | None
    artifact: CanonicalArtifact | None
    error_code: str | None = None
    error_message: str | None = None


class IndexPublisherClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float,
        session: ThreadLocalSessionPool | None = None,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session = session or ThreadLocalSessionPool()
        self.wall_clock = wall_clock

    def close(self) -> None:
        self.session.close()

    def publish(self, submission: PublicationSubmission) -> PublicationResult:
        try:
            response = self.session.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                },
                files={
                    "manifest": (
                        "manifest.json",
                        submission.manifest,
                        "application/json",
                    ),
                    "torrent": (
                        f"{submission.btih}.torrent",
                        submission.torrent,
                        "application/x-bittorrent",
                    ),
                },
                timeout=(5, self.timeout),
            )
        except requests.RequestException as error:
            raise RetryablePublicationError(
                "network_error",
                f"Index publication request failed: {error}",
            ) from error

        status = response.status_code
        if status == 429 or (500 <= status <= 599 and status != 503):
            raise RetryablePublicationError(
                f"http_{status}",
                f"Index publication returned HTTP {status}",
                retry_after=_retry_after(
                    response.headers.get("Retry-After"), self.wall_clock
                ),
            )
        if status in {401, 403, 404, 503}:
            raise PublicationPaused(
                f"http_{status}",
                f"Index publication is unavailable (HTTP {status})",
            )

        document = _json_document(response)
        if status in {200, 201}:
            return _terminal_outcome(document, status, submission)
        if status == 409 and document.get("outcome") == "artifact_duplicate":
            return _terminal_outcome(document, status, submission)
        if status in {400, 413, 409}:
            return _terminal_error(document, status)
        raise PublicationPaused(
            "unexpected_status",
            f"Index publication returned unsupported HTTP {status}",
        )


def _json_document(response: requests.Response) -> Mapping[str, Any]:
    try:
        document = response.json()
    except (requests.JSONDecodeError, ValueError) as error:
        raise PublicationPaused(
            "invalid_response",
            "Index publication returned invalid JSON",
        ) from error
    if not isinstance(document, Mapping):
        raise PublicationPaused(
            "invalid_response",
            "Index publication response must be a JSON object",
        )
    if document.get("schema") != RESPONSE_SCHEMA:
        raise PublicationPaused(
            "schema_mismatch",
            "Index publication response schema is unsupported",
        )
    return document


def _terminal_outcome(
    document: Mapping[str, Any],
    status: int,
    submission: PublicationSubmission,
) -> PublicationResult:
    allowed = {
        200: {"idempotent"},
        201: {"created", "promoted"},
        409: {"artifact_duplicate"},
    }[status]
    outcome = document.get("outcome")
    if outcome not in allowed:
        raise PublicationPaused(
            "invalid_response",
            f"Index publication HTTP {status} has an invalid outcome",
        )
    canonical = document.get("canonical")
    if not isinstance(canonical, bool):
        raise PublicationPaused(
            "invalid_response",
            "Index publication canonical flag is invalid",
        )
    receipt = _mapping(document, "receipt")
    expected_receipt = {
        "sd_hash": submission.sd_hash,
        "sha384": submission.sha384,
        "btih": submission.btih,
    }
    if any(receipt.get(key) != value for key, value in expected_receipt.items()):
        raise PublicationPaused(
            "receipt_mismatch",
            "Index publication receipt does not match the submission",
        )
    artifact = _canonical_artifact(document.get("canonical_artifact"))
    if artifact.sha384 != submission.sha384:
        raise PublicationPaused(
            "receipt_mismatch",
            "Index canonical artifact does not match the submitted SHA-384",
        )
    if canonical != (artifact.btih == submission.btih):
        raise PublicationPaused(
            "invalid_response",
            "Index canonical flag contradicts the canonical artifact",
        )
    state = (
        PublicationState.DUPLICATE
        if outcome == "artifact_duplicate"
        else PublicationState.PUBLISHED
    )
    return PublicationResult(
        state=state,
        outcome=outcome,
        canonical=canonical,
        artifact=artifact,
    )


def _terminal_error(
    document: Mapping[str, Any],
    status: int,
) -> PublicationResult:
    error = _mapping(document, "error")
    code = error.get("code")
    message = error.get("message")
    fields = error.get("fields")
    if (
        not isinstance(code, str)
        or not ERROR_CODE_RE.fullmatch(code)
        or not isinstance(message, str)
        or not message
        or len(message) > 4096
        or not isinstance(fields, list)
        or not all(isinstance(field, str) for field in fields)
    ):
        raise PublicationPaused(
            "invalid_response",
            "Index publication error response is malformed",
        )
    raw_artifact = document.get("canonical_artifact")
    artifact = None if raw_artifact is None else _canonical_artifact(raw_artifact)
    return PublicationResult(
        state=(
            PublicationState.CONFLICT if status == 409 else PublicationState.REJECTED
        ),
        outcome=None,
        canonical=None,
        artifact=artifact,
        error_code=code,
        error_message=message,
    )


def _mapping(document: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = document.get(key)
    if not isinstance(value, Mapping):
        raise PublicationPaused(
            "invalid_response",
            f"Index publication response {key} is invalid",
        )
    return value


def _canonical_artifact(value: Any) -> CanonicalArtifact:
    if not isinstance(value, Mapping):
        raise PublicationPaused(
            "invalid_response",
            "Index publication canonical artifact is missing",
        )
    sha384 = value.get("sha384")
    btih = value.get("btih")
    torrent_url = value.get("torrent_url")
    magnet_uri = value.get("magnet_uri")
    winning_release_id = value.get("winning_release_id")
    if not isinstance(sha384, str) or not SHA384_RE.fullmatch(sha384):
        raise PublicationPaused("invalid_response", "Canonical SHA-384 is invalid")
    if not isinstance(btih, str) or not BTIH_RE.fullmatch(btih):
        raise PublicationPaused("invalid_response", "Canonical BTIH is invalid")
    if not isinstance(winning_release_id, str) or not CLAIM_ID_RE.fullmatch(
        winning_release_id
    ):
        raise PublicationPaused(
            "invalid_response",
            "Canonical winning release ID is invalid",
        )
    _absolute_http_url(torrent_url, "canonical torrent URL")
    _canonical_magnet(magnet_uri, btih)
    return CanonicalArtifact(
        sha384=sha384,
        btih=btih,
        torrent_url=torrent_url,
        magnet_uri=magnet_uri,
        winning_release_id=winning_release_id,
    )


def _absolute_http_url(value: Any, label: str) -> None:
    if not isinstance(value, str) or len(value) > 4096:
        raise PublicationPaused("invalid_response", f"Index {label} is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise PublicationPaused("invalid_response", f"Index {label} is invalid")


def _canonical_magnet(value: Any, btih: str) -> None:
    if not isinstance(value, str) or len(value) > 16384:
        raise PublicationPaused("invalid_response", "Index canonical magnet is invalid")
    parsed = urlsplit(value)
    if parsed.scheme != "magnet" or parse_qs(parsed.query).get("xt") != [
        f"urn:btih:{btih}"
    ]:
        raise PublicationPaused("invalid_response", "Index canonical magnet is invalid")


def _retry_after(value: str | None, wall_clock: Callable[[], float]) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        seconds = deadline.timestamp() - wall_clock()
    if not math.isfinite(seconds):
        return None
    if seconds < 0:
        return 0
    return seconds


def encode_manifest(value: Mapping[str, Any]) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("publication manifest exceeds the Index size limit")
    return raw
