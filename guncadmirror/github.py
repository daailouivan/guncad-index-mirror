from __future__ import annotations

import logging
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
from .http_download import download_url_to_file
from .index_client import USER_AGENT
from .models import Release
from .paths import safe_component
from .progress import ActivityPhase, ActivityUpdate, NullProgressReporter, ProgressReporter
from .sessions import ThreadLocalSessionPool

GITHUB_API_URL = "https://api.github.com"
GITHUB_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)(?:/(?:releases/tag|archive/refs/tags)/(?P<tag>[^/?#]+))?"
)


class GitHubError(RuntimeError):
    """GitHub acquisition failed."""


class GitHubProtocolError(GitHubError):
    """GitHub returned unexpected metadata."""


class GitHubUnavailable(GitHubError):
    """GitHub operation failed or exhausted retries."""


@dataclass(frozen=True, slots=True)
class GitHubAcquisition:
    path: Path
    source_url: str | None = None


class GitHubAcquirer:
    def __init__(
        self,
        api_url: str = GITHUB_API_URL,
        token: str | None = None,
        session: requests.Session | ThreadLocalSessionPool | None = None,
        attempts: int = 5,
        backoff: float = 2.0,
        read_timeout: float = 60.0,
        logger: logging.Logger | None = None,
        progress: ProgressReporter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.session = session or ThreadLocalSessionPool()
        self.attempts = attempts
        self.backoff = backoff
        self.read_timeout = read_timeout
        self.logger = logger or logging.getLogger("guncad-mirror.github")
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
    ) -> GitHubAcquisition:
        check_cancelled(stop)
        self.progress.update_activity(
            ActivityUpdate(release=release, phase=ActivityPhase.GITHUB)
        )

        owner, repo, tag = self._parse_target(release)
        output_directory.mkdir(parents=True, exist_ok=True)

        download_url, file_name = self._resolve_download_target(
            owner, repo, tag, stop=stop
        )
        safe_name = safe_component(file_name, fallback=f"{repo}.zip")
        destination = output_directory / safe_name

        download_url_to_file(
            download_url,
            destination,
            session=self.session,
            attempts=self.attempts,
            backoff=self.backoff,
            read_timeout=self.read_timeout,
            stop=stop,
            sleep=self.sleep,
            logger=self.logger,
        )

        return GitHubAcquisition(path=destination, source_url=download_url)

    def _parse_target(self, release: Release) -> tuple[str, str, str | None]:
        url = release.url
        if url:
            match = GITHUB_REPO_RE.match(url)
            if match:
                owner = match.group("owner")
                repo = match.group("repo").removesuffix(".git")
                tag = match.group("tag")
                return owner, repo, tag

        ext_id = release.external_id or release.id.removeprefix("github-")
        parts = ext_id.split("/")
        if len(parts) >= 2:
            owner, repo = parts[0], parts[1]
            tag = parts[2] if len(parts) > 2 else None
            return owner, repo, tag

        raise GitHubProtocolError(
            f"cannot determine GitHub owner/repo from release {release.id}"
        )

    def _resolve_download_target(
        self,
        owner: str,
        repo: str,
        tag: str | None,
        *,
        stop: Event | None = None,
    ) -> tuple[str, str]:
        headers: dict[str, str] = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        # 1. If tag is given, inspect release assets
        endpoint = (
            f"{self.api_url}/repos/{owner}/{repo}/releases/tags/{tag}"
            if tag
            else f"{self.api_url}/repos/{owner}/{repo}/releases/latest"
        )

        try:
            check_cancelled(stop)
            response = self.session.get(
                endpoint,
                headers=headers,
                timeout=(10, self.read_timeout),
            )
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, Mapping):
                    assets = data.get("assets")
                    if isinstance(assets, list) and assets:
                        # Prefer zip archives or the largest asset
                        sorted_assets = sorted(
                            assets,
                            key=lambda a: (
                                str(a.get("name", "")).endswith(".zip"),
                                int(a.get("size", 0)),
                            ),
                            reverse=True,
                        )
                        target = sorted_assets[0]
                        download_url = target.get("browser_download_url")
                        asset_name = target.get("name")
                        if download_url and asset_name:
                            return str(download_url), str(asset_name)

                    zipball = data.get("zipball_url")
                    tag_name = data.get("tag_name", tag or "latest")
                    if zipball:
                        return str(zipball), f"{repo}-{tag_name}.zip"
        except requests.RequestException as error:
            self.logger.debug("Failed querying GitHub release endpoint %s: %s", endpoint, error)

        # Fallback to repo zipball archive
        ref = tag or "main"
        archive_url = f"{self.api_url}/repos/{owner}/{repo}/zipball/{ref}"
        return archive_url, f"{repo}-{ref}.zip"
