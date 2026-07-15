from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

DEFAULT_ENDPOINT = "https://guncadindex.com/api/v2/releases/?format=json&limit=100"
TRUTHY = frozenset({"1", "true", "t", "yes", "on", "enabled"})
FALSY = frozenset({"0", "false", "f", "no", "off", "disabled", ""})


class ConfigurationError(ValueError):
    """An environment variable cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class Settings:
    endpoint: str = DEFAULT_ENDPOINT
    data_dir: Path = Path("/data")
    lbry_url: str = "http://127.0.0.1:5279"
    api_max_pages: int = 1000
    max_releases_per_run: int | None = None
    max_release_size: int = 10 * 1024**3
    min_free_space: int = 5 * 1024**3
    loop_interval: float = 4 * 60 * 60
    lbry_startup_timeout: float = 5 * 60
    download_timeout: float = 60 * 60
    download_poll_interval: float = 2.0
    retry_attempts: int = 5
    retry_backoff: float = 2.0
    enable_webui: bool = False
    blacklisted_handles: tuple[str, ...] = ()
    torrent_piece_length: int = 1024**2
    torrent_trackers: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        max_releases = _integer(env, "MIRROR_MAX_RELEASES_PER_RUN", 0, minimum=0)
        settings = cls(
            endpoint=env.get("MIRROR_API_ENDPOINT", DEFAULT_ENDPOINT).strip(),
            data_dir=Path(env.get("MIRROR_DATA_DIR", "/data")),
            lbry_url=env.get("MIRROR_LBRY_URL", "http://127.0.0.1:5279").strip(),
            api_max_pages=_integer(env, "MIRROR_API_MAX_PAGES", 1000, minimum=1),
            max_releases_per_run=max_releases or None,
            max_release_size=_integer(
                env, "MIRROR_RELEASE_MAX_SIZE", 10 * 1024**3, minimum=0
            ),
            min_free_space=_integer(
                env, "MIRROR_MIN_FREE_SPACE", 5 * 1024**3, minimum=0
            ),
            loop_interval=_number(env, "MIRROR_LOOP_INTERVAL", 4 * 60 * 60, minimum=1),
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

    def validate(self) -> None:
        _validate_http_url(self.endpoint, "MIRROR_API_ENDPOINT")
        _validate_http_url(self.lbry_url, "MIRROR_LBRY_URL")
        if self.torrent_piece_length & (self.torrent_piece_length - 1):
            raise ConfigurationError(
                "MIRROR_TORRENT_PIECE_LENGTH must be a power of two"
            )
        for tracker in self.torrent_trackers:
            parsed = urlsplit(tracker)
            if parsed.scheme not in {"http", "https", "udp"} or not parsed.netloc:
                raise ConfigurationError(f"invalid torrent tracker URL: {tracker}")


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
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _list(value: str) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in value.replace("\n", ",").split(",") if item.strip()
    )
