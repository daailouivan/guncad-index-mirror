from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

import requests

from .state import JobStore

USER_AGENT = f"GunCADMirror/1.0 {requests.utils.default_user_agent()}"
MAX_POLICY_BYTES = 64 * 1024
MAX_TRACKERS = 256
MAX_TRACKER_URL = 2048
MAX_ETAG = 512


class TrackerPolicyError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TrackerState(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    BLACKLISTED = "blacklisted"


@dataclass(frozen=True, slots=True)
class TrackerPolicyEntry:
    url: str
    state: TrackerState
    position: int


@dataclass(frozen=True, slots=True)
class TrackerPolicy:
    strip_uploader_trackers: bool
    trackers: tuple[TrackerPolicyEntry, ...]

    @property
    def enabled(self) -> tuple[str, ...]:
        return tuple(
            tracker.url
            for tracker in self.trackers
            if tracker.state is TrackerState.ENABLED
        )

    @property
    def blacklisted(self) -> frozenset[str]:
        return frozenset(
            tracker.url
            for tracker in self.trackers
            if tracker.state is TrackerState.BLACKLISTED
        )


EMPTY_POLICY = TrackerPolicy(strip_uploader_trackers=False, trackers=())


@dataclass(frozen=True, slots=True)
class TrackerPolicyResponse:
    policy: TrackerPolicy
    etag: str
    document: bytes


@dataclass(frozen=True, slots=True)
class TrackerPolicyStatus:
    enabled: bool
    endpoint: str
    source: str
    removals_authoritative: bool
    desired_trackers: tuple[str, ...]
    enabled_index_trackers: int
    blacklisted_trackers: int
    etag: str | None
    cached_at: float | None
    last_checked_at: float | None
    last_success_at: float | None
    error_code: str | None
    error: str | None


class TrackerPolicyClient:
    def __init__(
        self,
        url: str,
        *,
        timeout: float,
        session: requests.Session | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.session = session or requests.Session()

    def close(self) -> None:
        self.session.close()

    def fetch(self, etag: str | None = None) -> TrackerPolicyResponse | None:
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if etag is not None:
            headers["If-None-Match"] = etag
        try:
            response = self.session.get(
                self.url,
                headers=headers,
                timeout=(5, self.timeout),
            )
        except requests.RequestException as error:
            raise TrackerPolicyError(
                "network_error",
                f"Index tracker policy request failed: {error}",
            ) from error

        status = response.status_code
        if status == 304:
            if etag is None:
                raise TrackerPolicyError(
                    "invalid_response",
                    "Index tracker policy returned 304 without a cached policy",
                )
            return None
        if status != 200:
            raise TrackerPolicyError(
                f"http_{status}",
                f"Index tracker policy returned HTTP {status}",
            )

        response_etag = response.headers.get("ETag")
        if not _valid_etag(response_etag):
            raise TrackerPolicyError(
                "invalid_response",
                "Index tracker policy returned an invalid ETag",
            )
        raw_length = response.headers.get("Content-Length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except ValueError as error:
                raise TrackerPolicyError(
                    "invalid_response",
                    "Index tracker policy returned an invalid Content-Length",
                ) from error
            if declared_length < 0 or declared_length > MAX_POLICY_BYTES:
                raise TrackerPolicyError(
                    "response_too_large",
                    "Index tracker policy exceeds the response size limit",
                )
        document = response.content
        if not document or len(document) > MAX_POLICY_BYTES:
            raise TrackerPolicyError(
                "response_too_large",
                "Index tracker policy is empty or exceeds the response size limit",
            )
        return TrackerPolicyResponse(
            policy=parse_tracker_policy(document),
            etag=response_etag,
            document=document,
        )


class TrackerPolicyManager:
    def __init__(
        self,
        store: JobStore,
        client: TrackerPolicyClient | None,
        *,
        operator_trackers: tuple[str, ...] = (),
        logger: logging.Logger | None = None,
        record_event: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.client = client
        self.operator_trackers = operator_trackers
        self.logger = logger or logging.getLogger("guncad-mirror.tracker-policy")
        self.record_event = record_event or (lambda _message: None)
        self.clock = clock
        self.policy = EMPTY_POLICY
        self.source = "disabled" if client is None else "empty"
        self.etag: str | None = None
        self.document: bytes | None = None
        self.cached_at: float | None = None
        self.last_checked_at: float | None = None
        self.last_success_at: float | None = None
        self.error_code: str | None = None
        self.error: str | None = None
        self._cache_saved = False
        if client is not None:
            self._load_cache()

    @property
    def desired_trackers(self) -> tuple[str, ...]:
        blacklisted = self.policy.blacklisted
        desired: list[str] = []
        for tracker in (*self.policy.enabled, *self.operator_trackers):
            if tracker not in blacklisted and tracker not in desired:
                desired.append(tracker)
        return tuple(desired)

    @property
    def removals_authoritative(self) -> bool:
        return self.client is None or self.source in {"cache", "remote"}

    @property
    def status(self) -> TrackerPolicyStatus:
        return TrackerPolicyStatus(
            enabled=self.client is not None,
            endpoint=self.client.url if self.client is not None else "",
            source=self.source,
            removals_authoritative=self.removals_authoritative,
            desired_trackers=self.desired_trackers,
            enabled_index_trackers=len(self.policy.enabled),
            blacklisted_trackers=len(self.policy.blacklisted),
            etag=self.etag,
            cached_at=self.cached_at,
            last_checked_at=self.last_checked_at,
            last_success_at=self.last_success_at,
            error_code=self.error_code,
            error=self.error,
        )

    def refresh(self) -> TrackerPolicy:
        if self.client is None:
            return self.policy
        self.last_checked_at = self.clock()
        try:
            response = self.client.fetch(self.etag)
        except TrackerPolicyError as error:
            self._record_error(error)
            return self.policy
        except Exception as error:  # pragma: no cover - defensive HTTP boundary
            self._record_error(
                TrackerPolicyError(
                    "policy_client_error",
                    f"Index tracker policy client failed: {type(error).__name__}: {error}",
                )
            )
            return self.policy

        self.last_success_at = self.clock()
        if response is not None:
            self.policy = response.policy
            self.etag = response.etag
            self.document = response.document
            self.source = "remote"
            self._cache_saved = False
        if (
            not self._cache_saved
            and self.etag is not None
            and self.document is not None
        ):
            try:
                cached = self.store.save_tracker_policy_cache(
                    self.client.url,
                    self.etag,
                    self.document,
                )
            except Exception as error:  # pragma: no cover - defensive storage boundary
                self._record_error(
                    TrackerPolicyError(
                        "cache_write_error",
                        f"Cannot persist Index tracker policy: {type(error).__name__}: {error}",
                    )
                )
                return self.policy
            self.cached_at = cached.updated_at
            self._cache_saved = True
        self._clear_error()
        return self.policy

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def _load_cache(self) -> None:
        if self.client is None:  # pragma: no cover - constructor invariant
            return
        try:
            cached = self.store.load_tracker_policy_cache(self.client.url)
        except Exception as error:  # pragma: no cover - defensive storage boundary
            self._record_error(
                TrackerPolicyError(
                    "cache_read_error",
                    f"Cannot read cached Index tracker policy: {type(error).__name__}: {error}",
                )
            )
            return
        if cached is None:
            return
        try:
            policy = parse_tracker_policy(cached.document)
        except TrackerPolicyError as error:
            self._record_error(
                TrackerPolicyError(
                    "cache_invalid",
                    f"Cached Index tracker policy is invalid: {error.message}",
                )
            )
            return
        if not _valid_etag(cached.etag):
            self._record_error(
                TrackerPolicyError(
                    "cache_invalid",
                    "Cached Index tracker policy has an invalid ETag",
                )
            )
            return
        self.policy = policy
        self.etag = cached.etag
        self.document = cached.document
        self.cached_at = cached.updated_at
        self.source = "cache"
        self._cache_saved = True

    def _record_error(self, error: TrackerPolicyError) -> None:
        changed = (self.error_code, self.error) != (error.code, error.message)
        self.error_code = error.code
        self.error = error.message
        if changed:
            self.logger.warning(
                "Tracker policy degraded (%s): %s",
                error.code,
                error.message,
            )
            self.record_event(
                f"TRACKER POLICY DEGRADED ({error.code}): {error.message}"
            )

    def _clear_error(self) -> None:
        if self.error_code is not None:
            self.logger.info("Tracker policy recovered")
            self.record_event("TRACKER POLICY RECOVERED")
        self.error_code = None
        self.error = None


def parse_tracker_policy(raw: bytes) -> TrackerPolicy:
    if not raw or len(raw) > MAX_POLICY_BYTES:
        raise TrackerPolicyError(
            "invalid_response",
            "Index tracker policy is empty or exceeds the response size limit",
        )
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrackerPolicyError(
            "invalid_response",
            "Index tracker policy is not valid JSON",
        ) from error
    if not isinstance(document, Mapping) or set(document) != {
        "strip_uploader_trackers",
        "trackers",
    }:
        raise TrackerPolicyError(
            "invalid_response",
            "Index tracker policy must contain the expected fields",
        )
    strip_uploader_trackers = document["strip_uploader_trackers"]
    raw_trackers = document["trackers"]
    if not isinstance(strip_uploader_trackers, bool) or not isinstance(
        raw_trackers, list
    ):
        raise TrackerPolicyError(
            "invalid_response",
            "Index tracker policy fields have invalid types",
        )
    if len(raw_trackers) > MAX_TRACKERS:
        raise TrackerPolicyError(
            "invalid_response",
            "Index tracker policy contains too many trackers",
        )

    trackers: list[TrackerPolicyEntry] = []
    seen_urls: set[str] = set()
    for raw_tracker in raw_trackers:
        tracker = _parse_tracker(raw_tracker)
        if tracker.url in seen_urls:
            raise TrackerPolicyError(
                "invalid_response",
                "Index tracker policy contains a duplicate tracker URL",
            )
        seen_urls.add(tracker.url)
        trackers.append(tracker)
    trackers.sort(key=lambda tracker: (tracker.position, tracker.url))
    return TrackerPolicy(
        strip_uploader_trackers=strip_uploader_trackers,
        trackers=tuple(trackers),
    )


def _parse_tracker(value: Any) -> TrackerPolicyEntry:
    if not isinstance(value, Mapping) or set(value) != {"url", "state", "position"}:
        raise TrackerPolicyError(
            "invalid_response",
            "Index tracker policy entry must contain the expected fields",
        )
    url = value["url"]
    state = value["state"]
    position = value["position"]
    if not isinstance(url, str) or not _valid_tracker_url(url):
        raise TrackerPolicyError(
            "invalid_response",
            "Index tracker policy contains an invalid tracker URL",
        )
    try:
        parsed_state = TrackerState(state)
    except (TypeError, ValueError) as error:
        raise TrackerPolicyError(
            "invalid_response",
            "Index tracker policy contains an invalid tracker state",
        ) from error
    if (
        isinstance(position, bool)
        or not isinstance(position, int)
        or not 0 <= position < 2**31
    ):
        raise TrackerPolicyError(
            "invalid_response",
            "Index tracker policy contains an invalid tracker position",
        )
    return TrackerPolicyEntry(url=url, state=parsed_state, position=position)


def _valid_tracker_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        bool(value)
        and len(value) <= MAX_TRACKER_URL
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        and parsed.scheme in {"http", "https", "udp"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
    )


def _valid_etag(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= MAX_ETAG
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )
