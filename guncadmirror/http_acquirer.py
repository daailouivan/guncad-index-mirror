from __future__ import annotations

import logging
import mimetypes
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from urllib.parse import parse_qs, unquote, urlsplit

import requests

from .cancellation import check_cancelled
from .http_download import download_url_to_file
from .index_client import USER_AGENT
from .models import AcquisitionTransport, Release
from .paths import safe_component
from .progress import (
    ActivityPhase,
    ActivityUpdate,
    NullProgressReporter,
    ProgressReporter,
)
from .sessions import ThreadLocalSessionPool

HTTP_USER_AGENT = f"GunCADMirror/1.0 (DirectHTTP) {USER_AGENT}"
HTTP_REQUEST_HEADERS = {
    "User-Agent": HTTP_USER_AGENT,
    "Accept": "*/*",
}


class HttpError(RuntimeError):
    """Direct HTTP acquisition failed."""


class HttpProtocolError(HttpError):
    """Direct HTTP target returned unexpected headers or structure."""


class HttpUnavailable(HttpError):
    """Direct HTTP server could not be reached or exhausted retries."""


@dataclass(frozen=True, slots=True)
class HttpAcquisition:
    path: Path
    source_url: str | None = None


def parse_content_disposition_filename(header: str | None) -> str | None:
    """Extract filename parameter from a Content-Disposition header."""
    if not header:
        return None
    # RFC 5987 / 6266 filename* takes precedence
    star_match = re.search(
        r"""filename\*\s*=\s*(?:UTF-8''|utf-8'')?([^;\s]+)""", header, re.IGNORECASE
    )
    if star_match:
        val = star_match.group(1).strip("\"'")
        return unquote(val) if val else None

    # Standard filename parameter
    match = re.search(
        r"""filename\s*=\s*(?:"([^"]+)"|'([^']+)'|([^;\s]+))""", header, re.IGNORECASE
    )
    if match:
        val = match.group(1) or match.group(2) or match.group(3)
        return unquote(val.strip()) if val else None
    return None


def parse_url_filename(url: str) -> str | None:
    """Extract a candidate filename with extension from a URL path or query string."""
    parsed = urlsplit(url)
    path_name = Path(unquote(parsed.path)).name
    if path_name and "." in path_name:
        return path_name

    qs = parse_qs(parsed.query)
    for key in ("filename", "file", "name"):
        if key in qs and qs[key]:
            val = qs[key][0]
            if val and "." in val:
                return Path(unquote(val)).name
    return None


class HttpAcquirer:
    def __init__(
        self,
        session: requests.Session | ThreadLocalSessionPool | None = None,
        attempts: int = 5,
        backoff: float = 2.0,
        read_timeout: float = 60.0,
        logger: logging.Logger | None = None,
        progress: ProgressReporter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.session = session or ThreadLocalSessionPool()
        self.attempts = attempts
        self.backoff = backoff
        self.read_timeout = read_timeout
        self.logger = logger or logging.getLogger("guncad-mirror.http")
        self.progress = progress or NullProgressReporter()
        self.sleep = sleep

    def close(self) -> None:
        if hasattr(self.session, "close"):
            self.session.close()

    def acquire(
        self,
        release: Release,
        output_directory: Path,
        *,
        stop: Event | None = None,
    ) -> HttpAcquisition:
        check_cancelled(stop)
        self.progress.update_activity(
            ActivityUpdate(
                release=release,
                phase=ActivityPhase.HTTP,
                transport=AcquisitionTransport.HTTP,
                total_bytes=release.size,
            )
        )

        url = self._resolve_url(release)
        output_directory.mkdir(parents=True, exist_ok=True)
        filename = self._determine_filename(url, release, stop=stop)
        safe_name = safe_component(filename, fallback=f"{release.id}.zip")
        destination = output_directory / safe_name

        def _on_progress(done: int, total: int | None) -> None:
            self.progress.update_activity(
                ActivityUpdate(
                    release=release,
                    phase=ActivityPhase.HTTP,
                    transport=AcquisitionTransport.HTTP,
                    completed_bytes=done,
                    total_bytes=total or release.size,
                )
            )

        download_url_to_file(
            url,
            destination,
            headers=HTTP_REQUEST_HEADERS,
            session=self.session,
            attempts=self.attempts,
            backoff=self.backoff,
            read_timeout=self.read_timeout,
            stop=stop,
            progress=_on_progress,
            sleep=self.sleep,
            logger=self.logger,
        )

        return HttpAcquisition(path=destination, source_url=url)

    def _resolve_url(self, release: Release) -> str:
        origin = release.raw.get("origin") if isinstance(release.raw, Mapping) else None
        links = origin.get("links") if isinstance(origin, Mapping) else None
        if isinstance(links, list):
            candidates: list[str] = []
            for item in links:
                if not isinstance(item, Mapping):
                    continue
                url = item.get("url")
                if not isinstance(url, str) or not url.strip():
                    continue
                parsed = urlsplit(url)
                if parsed.scheme.lower() not in {"http", "https"}:
                    continue
                if item.get("download") is True:
                    return url
                name = str(item.get("name", "")).lower()
                if name in {"download", "direct", "source", "file"}:
                    return url
                candidates.append(url)
            if candidates:
                return candidates[0]

        if release.url:
            parsed = urlsplit(release.url)
            if parsed.scheme.lower() in {"http", "https"}:
                return release.url

        raise HttpError(f"release {release.id} has no downloadable HTTP url")

    def _determine_filename(
        self,
        url: str,
        release: Release,
        *,
        stop: Event | None = None,
    ) -> str:
        # Check URL path or query params first
        url_name = parse_url_filename(url)
        if url_name:
            return url_name

        # If not obvious from URL, probe with HEAD to inspect Content-Disposition
        try:
            check_cancelled(stop)
            active_session = self.session or requests.Session()
            response = active_session.head(
                url,
                headers=HTTP_REQUEST_HEADERS,
                timeout=(5, 10),
                allow_redirects=True,
            )
            if response.status_code < 400:
                cd_name = parse_content_disposition_filename(
                    response.headers.get("Content-Disposition")
                )
                if cd_name:
                    return cd_name

                # Check final redirected URL
                redirected_name = parse_url_filename(response.url)
                if redirected_name:
                    return redirected_name

                # Try inferring extension from Content-Type
                content_type = (
                    response.headers.get("Content-Type", "").split(";")[0].strip()
                )
                if content_type:
                    ext = mimetypes.guess_extension(content_type)
                    if ext:
                        base = safe_component(release.name, fallback=release.id)
                        return f"{base}{ext}"
        except Exception as error:
            self.logger.debug(
                "Could not probe HTTP HEAD for %s filename: %s", url, error
            )

        # Fallback to sanitized release name with .zip extension
        fallback_base = safe_component(release.name, fallback=release.id)
        if not fallback_base.endswith(".zip"):
            return f"{fallback_base}.zip"
        return fallback_base
