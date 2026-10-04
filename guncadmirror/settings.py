from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit

DEFAULT_ENDPOINT = "https://guncadindex.com/api/v2/releases/?format=json&limit=100"
DEFAULT_ODYSEE_PROXY_URL = "https://api.na-backend.odysee.com/api/v1/proxy"
TRUTHY = frozenset({"1", "true", "t", "yes", "on", "enabled"})
FALSY = frozenset({"0", "false", "f", "no", "off", "disabled", ""})
QBIT_API_KEY_RE = re.compile(r"^qbt_[A-Za-z0-9]{28}$")


class ConfigurationError(ValueError):
    """An environment variable cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class Settings:
    endpoint: str = DEFAULT_ENDPOINT
    data_dir: Path = Path("/data")
    lbry_url: str = "http://127.0.0.1:5279"
    odysee_fallback: bool = True
    odysee_proxy_url: str = DEFAULT_ODYSEE_PROXY_URL
    lbry_concurrency: int = 4
    odysee_concurrency: int = 2
    printables_concurrency: int = 2
    github_concurrency: int = 2
    finalize_concurrency: int = 2
    api_max_pages: int = 1000
    max_releases_per_run: int | None = None
    max_release_size: int = 10 * 1024**3
    min_free_space: int = 5 * 1024**3
    loop_interval: float = 4 * 60 * 60
    cycle_error_interval: float = 60
    lbry_startup_timeout: float = 5 * 60
    download_timeout: float = 60 * 60
    download_poll_interval: float = 2.0
    retry_attempts: int = 5
    retry_backoff: float = 2.0
    enable_webui: bool = False
    blacklisted_handles: tuple[str, ...] = ()
    torrent_piece_length: int = 1024**2
    torrent_trackers: tuple[str, ...] = ()
    tracker_policy_url: str = ""
    tracker_policy_timeout: float = 15
    qbittorrent_enabled: bool = False
    qbittorrent_url: str = "http://qbittorrent:8080"
    qbittorrent_api_key: str = ""
    qbittorrent_username: str = ""
    qbittorrent_password: str = ""
    qbittorrent_data_dir: Path = Path("/downloads")
    qbittorrent_timeout: float = 15
    qbittorrent_ready_timeout: float = 120
    qbittorrent_poll_interval: float = 2
    qbittorrent_recheck_interval: float = 5 * 60
    qbittorrent_category: str = "guncad-mirror"
    qbittorrent_tag: str = "guncad-mirror"
    publish_enabled: bool = False
    publish_url: str = ""
    publish_token: str = ""
    publish_concurrency: int = 2
    publish_timeout: float = 60
    github_token: str = ""

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        max_releases = _integer(env, "MIRROR_MAX_RELEASES_PER_RUN", 0, minimum=0)
        settings = cls(
            endpoint=env.get("MIRROR_API_ENDPOINT", DEFAULT_ENDPOINT).strip(),
            data_dir=Path(env.get("MIRROR_DATA_DIR", "/data")),
            lbry_url=env.get("MIRROR_LBRY_URL", "http://127.0.0.1:5279").strip(),
            odysee_fallback=_boolean(env, "MIRROR_ODYSEE_FALLBACK", True),
            odysee_proxy_url=env.get(
                "MIRROR_ODYSEE_PROXY_URL", DEFAULT_ODYSEE_PROXY_URL
            ).strip(),
            lbry_concurrency=_integer(env, "MIRROR_LBRY_CONCURRENCY", 4, minimum=1),
            odysee_concurrency=_integer(env, "MIRROR_ODYSEE_CONCURRENCY", 2, minimum=1),
            printables_concurrency=_integer(
                env, "MIRROR_PRINTABLES_CONCURRENCY", 2, minimum=1
            ),
            github_concurrency=_integer(env, "MIRROR_GITHUB_CONCURRENCY", 2, minimum=1),
            finalize_concurrency=_integer(
                env, "MIRROR_FINALIZE_CONCURRENCY", 2, minimum=1
            ),
            api_max_pages=_integer(env, "MIRROR_API_MAX_PAGES", 1000, minimum=1),
            max_releases_per_run=max_releases or None,
            max_release_size=_integer(
                env, "MIRROR_RELEASE_MAX_SIZE", 10 * 1024**3, minimum=0
            ),
            min_free_space=_integer(
                env, "MIRROR_MIN_FREE_SPACE", 5 * 1024**3, minimum=0
            ),
            loop_interval=_number(env, "MIRROR_LOOP_INTERVAL", 4 * 60 * 60, minimum=1),
            cycle_error_interval=_number(
                env, "MIRROR_CYCLE_ERROR_INTERVAL", 60, minimum=1
            ),
            lbry_startup_timeout=_number(
                env, "MIRROR_LBRY_STARTUP_TIMEOUT", 5 * 60, minimum=1
            ),
            download_timeout=_number(
                env, "MIRROR_DOWNLOAD_TIMEOUT", 60 * 60, minimum=1
            ),
            download_poll_interval=_number(
                env, "MIRROR_DOWNLOAD_POLL_INTERVAL", 2, minimum=0.05
            ),
            retry_attempts=_integer(env, "MIRROR_RETRY_ATTEMPTS", 5, minimum=1),
            retry_backoff=_number(env, "MIRROR_RETRY_BACKOFF", 2, minimum=0),
            enable_webui=_boolean(env, "MIRROR_ENABLE_WEBUI", False),
            blacklisted_handles=_list(env.get("MIRROR_BLACKLISTED_HANDLES", "")),
            torrent_piece_length=_integer(
                env, "MIRROR_TORRENT_PIECE_LENGTH", 1024**2, minimum=16 * 1024
            ),
            torrent_trackers=_list(env.get("MIRROR_TORRENT_TRACKERS", "")),
            tracker_policy_url=env.get("MIRROR_TRACKER_POLICY_URL", "").strip(),
            tracker_policy_timeout=_number(
                env, "MIRROR_TRACKER_POLICY_TIMEOUT", 15, minimum=1
            ),
            qbittorrent_enabled=_boolean(env, "MIRROR_QBITTORRENT_ENABLED", False),
            qbittorrent_url=env.get(
                "MIRROR_QBITTORRENT_URL", "http://qbittorrent:8080"
            ).strip(),
            qbittorrent_api_key=env.get("MIRROR_QBITTORRENT_API_KEY", ""),
            qbittorrent_username=env.get("MIRROR_QBITTORRENT_USERNAME", ""),
            qbittorrent_password=env.get("MIRROR_QBITTORRENT_PASSWORD", ""),
            qbittorrent_data_dir=Path(
                env.get("MIRROR_QBITTORRENT_DATA_DIR", "/downloads")
            ),
            qbittorrent_timeout=_number(
                env, "MIRROR_QBITTORRENT_TIMEOUT", 15, minimum=1
            ),
            qbittorrent_ready_timeout=_number(
                env, "MIRROR_QBITTORRENT_READY_TIMEOUT", 120, minimum=1
            ),
            qbittorrent_poll_interval=_number(
                env, "MIRROR_QBITTORRENT_POLL_INTERVAL", 2, minimum=0.05
            ),
            qbittorrent_recheck_interval=_number(
                env, "MIRROR_QBITTORRENT_RECHECK_INTERVAL", 5 * 60, minimum=5
            ),
            qbittorrent_category=env.get(
                "MIRROR_QBITTORRENT_CATEGORY", "guncad-mirror"
            ).strip(),
            qbittorrent_tag=env.get("MIRROR_QBITTORRENT_TAG", "guncad-mirror").strip(),
            publish_enabled=_boolean(env, "MIRROR_PUBLISH_ENABLED", False),
            publish_url=env.get("MIRROR_PUBLISH_URL", "").strip(),
            publish_token=env.get("MIRROR_PUBLISH_TOKEN", ""),
            publish_concurrency=_integer(
                env, "MIRROR_PUBLISH_CONCURRENCY", 2, minimum=1
            ),
            publish_timeout=_number(env, "MIRROR_PUBLISH_TIMEOUT", 60, minimum=1),
            github_token=env.get("MIRROR_GITHUB_TOKEN", env.get("GITHUB_TOKEN", "")).strip(),
        )
        settings.validate()
        return settings

    @property
    def state_path(self) -> Path:
        return self.data_dir / "mirror-state.sqlite3"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def releases_dir(self) -> Path:
        return self.data_dir / "releases"

    @property
    def effective_tracker_policy_url(self) -> str:
        if self.tracker_policy_url:
            return self.tracker_policy_url
        if not self.publish_url:
            return ""
        parsed = urlsplit(self.publish_url)
        base = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path.rstrip("/") + "/",
                "",
                "",
            )
        )
        return urljoin(
            base,
            "../tracker-policy/",
        )

    def validate(self) -> None:
        _validate_http_url(self.endpoint, "MIRROR_API_ENDPOINT")
        _validate_http_url(self.lbry_url, "MIRROR_LBRY_URL")
        _validate_http_url(self.odysee_proxy_url, "MIRROR_ODYSEE_PROXY_URL")
        if self.torrent_piece_length & (self.torrent_piece_length - 1):
            raise ConfigurationError(
                "MIRROR_TORRENT_PIECE_LENGTH must be a power of two"
            )
        for tracker in self.torrent_trackers:
            parsed = urlsplit(tracker)
            if (
                len(tracker) > 2048
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in tracker
                )
                or parsed.scheme not in {"http", "https", "udp"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                raise ConfigurationError(f"invalid torrent tracker URL: {tracker}")
        if self.tracker_policy_url:
            _validate_http_url(
                self.tracker_policy_url,
                "MIRROR_TRACKER_POLICY_URL",
            )
        _validate_http_url(self.qbittorrent_url, "MIRROR_QBITTORRENT_URL")
        if not self.qbittorrent_data_dir.is_absolute():
            raise ConfigurationError(
                "MIRROR_QBITTORRENT_DATA_DIR must be an absolute path"
            )
        if self.qbittorrent_api_key and not QBIT_API_KEY_RE.fullmatch(
            self.qbittorrent_api_key
        ):
            raise ConfigurationError(
                "MIRROR_QBITTORRENT_API_KEY must be a qBittorrent qbt_ API key"
            )
        credentials_present = bool(
            self.qbittorrent_username or self.qbittorrent_password
        )
        if self.qbittorrent_api_key and credentials_present:
            raise ConfigurationError(
                "configure either qBittorrent API-key or password authentication, not both"
            )
        if self.qbittorrent_enabled and not self.qbittorrent_api_key:
            if not self.qbittorrent_username or not self.qbittorrent_password:
                raise ConfigurationError(
                    "qBittorrent username and password are required when API-key authentication is not configured"
                )
        for name, value in (
            ("MIRROR_QBITTORRENT_USERNAME", self.qbittorrent_username),
            ("MIRROR_QBITTORRENT_PASSWORD", self.qbittorrent_password),
        ):
            if len(value) > 4096 or "\x00" in value or "\r" in value or "\n" in value:
                raise ConfigurationError(f"{name} contains an invalid value")
        for name, value in (
            ("MIRROR_QBITTORRENT_CATEGORY", self.qbittorrent_category),
            ("MIRROR_QBITTORRENT_TAG", self.qbittorrent_tag),
        ):
            if not value or len(value) > 128 or "," in value:
                raise ConfigurationError(
                    f"{name} must be 1-128 characters without commas"
                )
        if self.publish_enabled:
            if not self.qbittorrent_enabled:
                raise ConfigurationError(
                    "MIRROR_QBITTORRENT_ENABLED is required when publication is enabled"
                )
            _validate_http_url(self.publish_url, "MIRROR_PUBLISH_URL")
            if not self.publish_token:
                raise ConfigurationError(
                    "MIRROR_PUBLISH_TOKEN is required when publication is enabled"
                )
        elif self.publish_url:
            _validate_http_url(self.publish_url, "MIRROR_PUBLISH_URL")
        if self.publish_token and (
            len(self.publish_token) > 4096
            or any(
                character.isspace() or ord(character) < 32
                for character in self.publish_token
            )
        ):
            raise ConfigurationError(
                "MIRROR_PUBLISH_TOKEN must be a non-whitespace bearer value"
            )


def _validate_http_url(value: str, name: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ConfigurationError(f"{name} must not contain embedded credentials")


def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    if name not in env:
        return default
    value = env[name].strip().lower()
    if value in TRUTHY:
        return True
    if value in FALSY:
        return False
    raise ConfigurationError(f"{name} must be a boolean value")


def _integer(env: Mapping[str, str], name: str, default: int, *, minimum: int) -> int:
    raw = env.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _number(
    env: Mapping[str, str], name: str, default: float, *, minimum: float
) -> float:
    raw = env.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be numeric") from error
    if not math.isfinite(value):
        raise ConfigurationError(f"{name} must be finite")
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _list(value: str) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in value.replace("\n", ",").split(",") if item.strip()
    )
