from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Mapping
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import requests

from .models import Release, ReleaseValidationError, UnsupportedOriginError

USER_AGENT = f"GunCADMirror/1.0 {requests.utils.default_user_agent()}"


class IndexError(RuntimeError):
    """The configured Index endpoint returned an unusable response."""


class IndexClient:
    def __init__(
        self,
        endpoint: str,
        *,
        max_pages: int,
        max_releases: int | None,
        attempts: int = 1,
        backoff: float = 0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ):
        self.endpoint = endpoint
        self.max_pages = max_pages
        self.max_releases = max_releases
        self.attempts = attempts
        self.backoff = backoff
        self.session = session or requests.Session()
        self.sleep = sleep
        self.logger = logger or logging.getLogger("guncad-mirror.index")
        self._origin = _origin(endpoint)

    def releases(self) -> Iterator[Release]:
        url: str | None = self.endpoint
        seen_urls: set[str] = set()
        yielded = 0

        for page_number in range(1, self.max_pages + 1):
            if url is None:
                return
            if url in seen_urls:
                raise IndexError(f"Index pagination loop detected at {url}")
            seen_urls.add(url)

            payload = self._fetch_page(url, page_number)
            if not isinstance(payload, Mapping):
                raise IndexError(f"Index page {page_number} must be a JSON object")
            results = payload.get("results")
            if not isinstance(results, list):
                raise IndexError(f"Index page {page_number} has no results list")

            for raw_release in results:
                try:
                    release = Release.from_api(raw_release)
                except UnsupportedOriginError as error:
                    self.logger.info("Skipping Index release: %s", error)
                    continue
                except ReleaseValidationError as error:
                    self.logger.error("Skipping malformed Index release: %s", error)
                    continue
                yield release
                yielded += 1
                if self.max_releases is not None and yielded >= self.max_releases:
                    return

            url = self._next_url(payload.get("next"), url)

        if url is not None:
            self.logger.warning(
                "Stopped at configured API page limit (%d); more pages remain",
                self.max_pages,
            )

    def _fetch_page(self, url: str, page_number: int) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.session.get(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=(5, 60),
                )
                response.raise_for_status()
                return response.json()
            except requests.RequestException as error:
                last_error = error
                if attempt == self.attempts:
                    break
                delay = self.backoff * (2 ** (attempt - 1))
                self.logger.warning(
                    "Index page %d attempt %d/%d failed: %s; retrying in %.1fs",
                    page_number,
                    attempt,
                    self.attempts,
                    error,
                    delay,
                )
                self.sleep(delay)
        raise IndexError(
            f"Index page {page_number} failed after {self.attempts} attempts: {last_error}"
        ) from last_error

    def _next_url(self, value: Any, current_url: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise IndexError("Index next link must be a URL or null")
        next_url = urljoin(current_url, value)
        if _origin(next_url) != self._origin:
            raise IndexError(
                f"Index pagination attempted to leave configured origin: {next_url}"
            )
        return next_url


def _origin(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), parsed.netloc.lower()
