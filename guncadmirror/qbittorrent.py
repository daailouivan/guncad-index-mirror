from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

UPLOAD_STATES = frozenset({"forcedUP", "stalledUP", "uploading"})
CONNECTED_STATES = frozenset({"connected", "firewalled"})


class QBitError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class QBitRetryableError(QBitError):
    """qBittorrent may become usable without configuration changes."""


class QBitConfigurationError(QBitError):
    """The configured qBittorrent endpoint cannot satisfy Mirror's contract."""


class QBitArtifactError(QBitError):
    """qBittorrent's record for one torrent contradicts Mirror's artifact."""


@dataclass(frozen=True, slots=True)
class QBitTransfer:
    connection_status: str
    dht_nodes: int


@dataclass(frozen=True, slots=True)
class QBitTorrent:
    info_hash: str
    content_path: str
    save_path: str
    progress: float
    amount_left: int
    total_size: int
    state: str
    force_start: bool
    category: str
    tags: tuple[str, ...]

    @property
    def upload_capable(self) -> bool:
        return (
            self.progress == 1.0
            and self.amount_left == 0
            and self.state in UPLOAD_STATES
            and self.force_start
        )


@dataclass(frozen=True, slots=True)
class QBitTracker:
    url: str
    status: int
    tier: int

    @property
    def is_network_tracker(self) -> bool:
        return self.tier >= 0


@dataclass(frozen=True, slots=True)
class QBitObservation:
    torrent: QBitTorrent
    transfer: QBitTransfer
    trackers: tuple[QBitTracker, ...]

    @property
    def working_trackers(self) -> int:
        return sum(tracker.status == 2 for tracker in self.trackers)

    @property
    def discovery_ready(self) -> bool:
        return self.transfer.connection_status in CONNECTED_STATES and (
            self.transfer.dht_nodes > 0 or self.working_trackers > 0
        )

    @property
    def green(self) -> bool:
        return self.torrent.upload_capable and self.discovery_ready


class QBitClient:
    def __init__(
        self,
        url: str,
        *,
        timeout: float,
        api_key: str = "",
        username: str = "",
        password: str = "",
        session: requests.Session | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        parsed = urlsplit(self.url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.timeout = timeout
        self.api_key = api_key
        self.username = username
        self.password = password
        self.session = session or requests.Session()
        self._authenticated = False

    def close(self) -> None:
        self.session.close()

    def versions(self) -> tuple[str, str]:
        version = self._text("GET", "/api/v2/app/version")
        webapi = self._text("GET", "/api/v2/app/webapiVersion")
        if not version.startswith("v") or not _valid_version(webapi):
            raise QBitConfigurationError(
                "invalid_version",
                "qBittorrent returned invalid application or WebAPI version data",
            )
        return version, webapi

    def transfer(self) -> QBitTransfer:
        document = self._json("GET", "/api/v2/transfer/info")
        if not isinstance(document, Mapping):
            raise self._invalid_response("transfer info must be an object")
        connection_status = document.get("connection_status")
        dht_nodes = document.get("dht_nodes")
        if connection_status not in {"connected", "firewalled", "disconnected"}:
            raise self._invalid_response("connection status is invalid")
        if isinstance(dht_nodes, bool) or not isinstance(dht_nodes, int):
            raise self._invalid_response("DHT node count is invalid")
        if dht_nodes < 0:
            raise self._invalid_response("DHT node count is negative")
        return QBitTransfer(connection_status, dht_nodes)

    def torrent(self, info_hash: str) -> QBitTorrent | None:
        document = self._json(
            "GET",
            "/api/v2/torrents/info",
            params={"hashes": info_hash},
        )
        if not isinstance(document, list):
            raise self._invalid_response("torrent lookup must return a list")
        if not document:
            return None
        if len(document) != 1 or not isinstance(document[0], Mapping):
            raise self._invalid_response("torrent lookup returned ambiguous data")
        value = document[0]
        returned_hash = value.get("hash")
        content_path = value.get("content_path")
        save_path = value.get("save_path")
        progress = value.get("progress")
        amount_left = value.get("amount_left")
        total_size = value.get("size")
        state = value.get("state")
        force_start = value.get("force_start")
        category = value.get("category")
        raw_tags = value.get("tags")
        if not isinstance(returned_hash, str) or returned_hash.lower() != info_hash:
            raise self._invalid_response("torrent lookup returned the wrong hash")
        if not _bounded_string(content_path) or not _bounded_string(save_path):
            raise self._invalid_response("torrent paths are invalid")
        if (
            isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not math.isfinite(progress)
            or not 0 <= progress <= 1
        ):
            raise self._invalid_response("torrent progress is invalid")
        if (
            isinstance(amount_left, bool)
            or not isinstance(amount_left, int)
            or amount_left < 0
        ):
            raise self._invalid_response("torrent remaining byte count is invalid")
        if (
            isinstance(total_size, bool)
            or not isinstance(total_size, int)
            or total_size <= 0
        ):
            raise self._invalid_response("torrent size is invalid")
        if not _bounded_string(state, maximum=128) or not isinstance(force_start, bool):
            raise self._invalid_response("torrent state is invalid")
        if not _optional_bounded_string(category, maximum=128):
            raise self._invalid_response("torrent category is invalid")
        if not _optional_bounded_string(raw_tags):
            raise self._invalid_response("torrent tags are invalid")
        tags = tuple(tag.strip() for tag in raw_tags.split(",") if tag.strip())
        return QBitTorrent(
            info_hash=returned_hash.lower(),
            content_path=content_path,
            save_path=save_path,
            progress=float(progress),
            amount_left=amount_left,
            total_size=total_size,
            state=state,
            force_start=force_start,
            category=category,
            tags=tags,
        )

    def add(
        self,
        torrent_path: Path,
        *,
        save_path: str,
        category: str,
        tag: str,
    ) -> None:
        try:
            torrent = torrent_path.read_bytes()
        except OSError as error:
            raise QBitArtifactError(
                "torrent_unreadable",
                f"Cannot read torrent metainfo: {error}",
            ) from error
        response = self._request(
            "POST",
            "/api/v2/torrents/add",
            data={
                "savepath": save_path,
                "category": category,
                "tags": tag,
                "skip_checking": "true",
                "paused": "false",
                "autoTMM": "false",
                "ratioLimit": "-1",
                "seedingTimeLimit": "-1",
            },
            files={
                "torrents": (
                    torrent_path.name,
                    torrent,
                    "application/x-bittorrent",
                )
            },
        )
        if response.status_code == 415:
            raise QBitArtifactError(
                "torrent_rejected",
                "qBittorrent rejected the generated torrent metainfo",
            )
        self._require_success(response)

    def add_url(
        self,
        urls: str,
        *,
        save_path: str,
        category: str = "",
        tag: str = "",
        paused: bool = False,
    ) -> None:
        response = self._request(
            "POST",
            "/api/v2/torrents/add",
            data={
                "urls": urls,
                "savepath": save_path,
                "category": category,
                "tags": tag,
                "skip_checking": "false",
                "paused": "true" if paused else "false",
                "autoTMM": "false",
                "ratioLimit": "-1",
                "seedingTimeLimit": "-1",
            },
        )
        self._require_success(response)

    def delete(
        self,
        info_hash: str,
        *,
        delete_files: bool = False,
    ) -> None:
        response = self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={
                "hashes": info_hash,
                "deleteFiles": "true" if delete_files else "false",
            },
        )
        self._require_success(response)

    def force_start(self, info_hash: str) -> None:
        response = self._request(
            "POST",
            "/api/v2/torrents/setForceStart",
            data={"hashes": info_hash, "value": "true"},
        )
        self._require_success(response)

    def reannounce(self, info_hash: str) -> None:
        response = self._request(
            "POST",
            "/api/v2/torrents/reannounce",
            data={"hashes": info_hash},
        )
        self._require_success(response)

    def add_trackers(self, info_hash: str, trackers: tuple[str, ...]) -> None:
        if not trackers:
            return
        response = self._request(
            "POST",
            "/api/v2/torrents/addTrackers",
            data={"hash": info_hash, "urls": "\n".join(trackers)},
        )
        if response.status_code == 409:
            return
        if response.status_code == 404:
            raise QBitRetryableError(
                "torrent_missing",
                "qBittorrent lost the torrent while adding tracker hints",
            )
        self._require_success(response)

    def remove_trackers(self, info_hash: str, trackers: tuple[str, ...]) -> None:
        if not trackers:
            return
        response = self._request(
            "POST",
            "/api/v2/torrents/removeTrackers",
            data={"hash": info_hash, "urls": "|".join(trackers)},
        )
        if response.status_code == 409:
            return
        if response.status_code == 404:
            raise QBitRetryableError(
                "torrent_missing",
                "qBittorrent lost the torrent while removing tracker hints",
            )
        self._require_success(response)

    def trackers(self, info_hash: str) -> tuple[QBitTracker, ...]:
        document = self._json(
            "GET",
            "/api/v2/torrents/trackers",
            params={"hash": info_hash},
        )
        if not isinstance(document, list):
            raise self._invalid_response("tracker lookup must return a list")
        trackers: list[QBitTracker] = []
        for item in document:
            if not isinstance(item, Mapping):
                raise self._invalid_response("tracker entry must be an object")
            url = item.get("url")
            status = item.get("status")
            tier = item.get("tier")
            if not _bounded_string(url):
                raise self._invalid_response("tracker URL is invalid")
            if (
                isinstance(status, bool)
                or not isinstance(status, int)
                or not 0 <= status <= 4
            ):
                raise self._invalid_response("tracker status is invalid")
            if isinstance(tier, bool) or not isinstance(tier, int) or tier < -1:
                raise self._invalid_response("tracker tier is invalid")
            trackers.append(QBitTracker(url=url, status=status, tier=tier))
        return tuple(trackers)

    def observe(self, info_hash: str) -> QBitObservation | None:
        torrent = self.torrent(info_hash)
        if torrent is None:
            return None
        return QBitObservation(
            torrent=torrent,
            transfer=self.transfer(),
            trackers=self.trackers(info_hash),
        )

    def _authenticate(self) -> None:
        if self.api_key:
            self._authenticated = True
            return
        try:
            response = self.session.post(
                self._url("/api/v2/auth/login"),
                headers={"Origin": self.origin, "Accept": "text/plain"},
                data={"username": self.username, "password": self.password},
                timeout=(5, self.timeout),
            )
        except requests.RequestException as error:
            raise QBitRetryableError(
                "network_error",
                f"qBittorrent authentication request failed: {error}",
            ) from error
        if response.status_code not in {200, 204}:
            raise QBitConfigurationError(
                "authentication_failed",
                f"qBittorrent rejected its configured credentials (HTTP {response.status_code})",
            )
        self._authenticated = True

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        headers = {"Origin": self.origin, "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(kwargs.pop("headers", {}))
        for attempt in range(2):
            if not self._authenticated:
                self._authenticate()
            try:
                response = self.session.request(
                    method,
                    self._url(path),
                    headers=headers,
                    timeout=(5, self.timeout),
                    **kwargs,
                )
            except requests.RequestException as error:
                raise QBitRetryableError(
                    "network_error",
                    f"qBittorrent request failed: {error}",
                ) from error
            if response.status_code not in {401, 403}:
                return response
            self._authenticated = False
            if not self.api_key:
                self.session.cookies.clear()
            if self.api_key or attempt == 1:
                raise QBitConfigurationError(
                    "authentication_failed",
                    "qBittorrent rejected the configured authentication "
                    f"(HTTP {response.status_code})",
                )
        raise AssertionError("unreachable authentication retry state")

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._request(method, path, **kwargs)
        self._require_success(response)
        try:
            return response.json()
        except (requests.JSONDecodeError, ValueError) as error:
            raise self._invalid_response("response is not valid JSON") from error

    def _text(self, method: str, path: str) -> str:
        response = self._request(method, path)
        self._require_success(response)
        value = response.text.strip()
        if not value or len(value) > 256:
            raise self._invalid_response("text response is invalid")
        return value

    def _require_success(self, response: requests.Response) -> None:
        status = response.status_code
        if 200 <= status <= 299:
            return
        if status == 429 or 500 <= status <= 599:
            raise QBitRetryableError(
                f"http_{status}",
                f"qBittorrent is unavailable (HTTP {status})",
            )
        raise QBitConfigurationError(
            f"http_{status}",
            f"qBittorrent returned unsupported HTTP {status}",
        )

    def _url(self, path: str) -> str:
        return f"{self.url}{path}"

    @staticmethod
    def _invalid_response(detail: str) -> QBitConfigurationError:
        return QBitConfigurationError(
            "invalid_response",
            f"qBittorrent {detail}",
        )


def _bounded_string(value: Any, *, maximum: int = 4096) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= maximum


def _optional_bounded_string(value: Any, *, maximum: int = 4096) -> bool:
    return isinstance(value, str) and len(value) <= maximum


def _valid_version(value: str) -> bool:
    parts = value.split(".")
    return len(parts) >= 2 and all(part.isdigit() for part in parts)
