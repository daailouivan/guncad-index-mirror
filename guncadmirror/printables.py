from __future__ import annotations

import logging
import re
import shutil
import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import Any

import requests

from .cancellation import check_cancelled, wait_or_cancel
from .http_download import download_url_to_file
from .models import Release
from .paths import safe_component
from .progress import (
    ActivityPhase,
    ActivityUpdate,
    NullProgressReporter,
    ProgressReporter,
)
from .sessions import ThreadLocalSessionPool

PRINTABLES_GRAPHQL_ENDPOINT = "https://api.printables.com/graphql/"
MODEL_URL_RE = re.compile(
    r"https?://(?:www\.)?printables\.com/model/(?P<id>\d+)(?:-(?P<slug>[^/?]+))?"
)
PRINTABLES_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
PRINTABLES_API_HEADERS = {
    "User-Agent": PRINTABLES_USER_AGENT,
    "Origin": "https://www.printables.com",
    "Referer": "https://www.printables.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}
PRINTABLES_DOWNLOAD_HEADERS = {
    "User-Agent": PRINTABLES_USER_AGENT,
    "Referer": "https://www.printables.com/",
    "Accept": "*/*",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
}


class PrintablesError(RuntimeError):
    """Printables could not supply the release assets."""


class PrintablesProtocolError(PrintablesError):
    """Printables returned an unexpected or malformed response."""


class PrintablesUnavailable(PrintablesError):
    """Printables operation failed or exhausted retries."""


@dataclass(frozen=True, slots=True)
class PrintablesAcquisition:
    path: Path
    source_url: str | None = None


class PrintablesAcquirer:
    def __init__(
        self,
        api_url: str = PRINTABLES_GRAPHQL_ENDPOINT,
        session: requests.Session | ThreadLocalSessionPool | None = None,
        attempts: int = 5,
        backoff: float = 2.0,
        read_timeout: float = 60.0,
        pacing: float = 2.0,
        logger: logging.Logger | None = None,
        progress: ProgressReporter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.api_url = api_url
        self.session = session or ThreadLocalSessionPool()
        self.attempts = attempts
        self.backoff = backoff
        self.read_timeout = read_timeout
        self.pacing = pacing
        self.logger = logger or logging.getLogger("guncad-mirror.printables")
        self.progress = progress or NullProgressReporter()
        self.sleep = sleep
        self._rate_limit_lock = Lock()
        self._next_allowed_request_time: float = 0.0

    def _schedule_request_slot(
        self,
        *,
        stop: Event | None = None,
    ) -> None:
        if self.pacing <= 0:
            with self._rate_limit_lock:
                now = time.monotonic()
                if self._next_allowed_request_time <= now:
                    return
                wait_time = self._next_allowed_request_time - now
            wait_or_cancel(stop, wait_time, sleep=self.sleep)
            return

        with self._rate_limit_lock:
            now = time.monotonic()
            scheduled_time = max(now, self._next_allowed_request_time)
            wait_time = scheduled_time - now
            if self._next_allowed_request_time == 0.0:
                wait_time = max(wait_time, self.pacing)
                scheduled_time = now + wait_time
            self._next_allowed_request_time = scheduled_time + self.pacing

        if wait_time > 0:
            wait_or_cancel(stop, wait_time, sleep=self.sleep)

    def _apply_rate_limit_cooldown(
        self,
        cooldown: float,
        *,
        stop: Event | None = None,
    ) -> None:
        with self._rate_limit_lock:
            now = time.monotonic()
            self._next_allowed_request_time = max(
                self._next_allowed_request_time,
                now + cooldown,
            )
        wait_or_cancel(stop, cooldown, sleep=self.sleep)
        with self._rate_limit_lock:
            now = time.monotonic()
            self._next_allowed_request_time = now

    def close(self) -> None:
        if hasattr(self.session, "close"):
            self.session.close()

    def acquire(
        self,
        release: Release,
        output_directory: Path,
        *,
        stop: Event | None = None,
    ) -> PrintablesAcquisition:
        check_cancelled(stop)
        self.progress.update_activity(
            ActivityUpdate(release=release, phase=ActivityPhase.PRINTABLES)
        )

        model_id = self._extract_model_id(release)
        files = self._fetch_model_files(model_id, stop=stop)
        if not files:
            raise PrintablesProtocolError(
                f"Printables model {model_id} has no downloadable files"
            )

        output_directory.mkdir(parents=True, exist_ok=True)

        if len(files) == 1:
            file_meta = files[0]
            link = self._get_download_link(
                file_meta["id"], model_id, file_meta["type"], stop=stop
            )
            dest_name = safe_component(file_meta["name"], fallback=f"{model_id}.stl")
            destination = output_directory / dest_name
            download_url_to_file(
                link,
                destination,
                headers=PRINTABLES_DOWNLOAD_HEADERS,
                session=self.session,
                attempts=self.attempts,
                backoff=self.backoff,
                read_timeout=self.read_timeout,
                stop=stop,
                sleep=self.sleep,
                logger=self.logger,
            )
            return PrintablesAcquisition(path=destination, source_url=link)

        # Multi-file model: download parts and package into a deterministic zip
        temp_dir = output_directory / f".tmp_{model_id}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        downloaded_entries: list[tuple[str, Path]] = []
        seen_names: dict[str, int] = {}
        primary_link: str | None = None

        try:
            for idx, file_meta in enumerate(files):
                check_cancelled(stop)
                link = self._get_download_link(
                    file_meta["id"], model_id, file_meta["type"], stop=stop
                )
                if primary_link is None:
                    primary_link = link
                raw_name = safe_component(file_meta["name"], fallback=f"file_{idx}.stl")
                count = seen_names.get(raw_name, 0)
                seen_names[raw_name] = count + 1
                if count > 0:
                    p = Path(raw_name)
                    file_dest_name = f"{p.stem}_{count}{p.suffix}"
                else:
                    file_dest_name = raw_name
                file_dest = temp_dir / file_dest_name
                download_url_to_file(
                    link,
                    file_dest,
                    headers=PRINTABLES_DOWNLOAD_HEADERS,
                    session=self.session,
                    attempts=self.attempts,
                    backoff=self.backoff,
                    read_timeout=self.read_timeout,
                    stop=stop,
                    sleep=self.sleep,
                    logger=self.logger,
                )
                downloaded_entries.append((file_dest_name, file_dest))
                if idx < len(files) - 1:
                    wait_or_cancel(stop, 0.5, sleep=self.sleep)

            zip_name = (
                f"{safe_component(release.name, fallback=f'printables-{model_id}')}.zip"
            )
            zip_destination = output_directory / zip_name
            _create_deterministic_zip(downloaded_entries, zip_destination)
            return PrintablesAcquisition(
                path=zip_destination, source_url=release.url or primary_link
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _extract_model_id(self, release: Release) -> str:
        if release.external_id and release.external_id.isdigit():
            return release.external_id
        if release.id.startswith("printables-"):
            clean_id = release.id.removeprefix("printables-")
            if clean_id.isdigit():
                return clean_id
        if release.url:
            match = MODEL_URL_RE.match(release.url)
            if match:
                return match.group("id")
        raise PrintablesProtocolError(
            f"cannot determine Printables model ID from release {release.id}"
        )

    def _graphql_post(
        self,
        query: str,
        variables: dict[str, Any],
        operation_name: str,
        *,
        stop: Event | None = None,
    ) -> dict[str, Any]:
        payload = {
            "operationName": operation_name,
            "query": query,
            "variables": variables,
        }
        last_error: Exception | None = None

        attempt = 1
        max_attempts = self.attempts
        while attempt <= max_attempts:
            check_cancelled(stop)
            self._schedule_request_slot(stop=stop)
            try:
                response = self.session.post(
                    self.api_url,
                    json=payload,
                    headers={
                        **PRINTABLES_API_HEADERS,
                        "Content-Type": "application/json",
                    },
                    timeout=(10, self.read_timeout),
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, Mapping):
                    raise PrintablesProtocolError(
                        "GraphQL response must be a JSON object"
                    )
                if "errors" in data and not data.get("data"):
                    raise PrintablesProtocolError(f"GraphQL errors: {data['errors']}")
                return data.get("data", {})
            except (requests.RequestException, ValueError) as error:
                last_error = error
                is_rate_limit = False
                retry_after_delay: float | None = None

                if isinstance(error, requests.HTTPError) and error.response is not None:
                    if error.response.status_code == 429:
                        is_rate_limit = True
                        resp_headers = getattr(error.response, "headers", None) or {}
                        retry_after = resp_headers.get("Retry-After")
                        if retry_after:
                            try:
                                retry_after_delay = float(retry_after)
                            except (ValueError, TypeError):
                                pass

                if is_rate_limit:
                    max_attempts = max(max_attempts, 7)

                if attempt >= max_attempts:
                    break

                if is_rate_limit and self.backoff > 0:
                    if retry_after_delay is not None:
                        delay = max(retry_after_delay, 5.0)
                    else:
                        delay = max(60.0, 60.0 * (2 ** (attempt - 1)))
                elif self.backoff > 0:
                    delay = self.backoff * (2 ** (attempt - 1))
                else:
                    delay = 0.0

                self.logger.warning(
                    "Printables %s attempt %d/%d failed: %s; retrying in %.1fs",
                    operation_name,
                    attempt,
                    max_attempts,
                    error,
                    delay,
                )
                if is_rate_limit and delay > 0:
                    self._apply_rate_limit_cooldown(delay, stop=stop)
                elif delay > 0:
                    wait_or_cancel(stop, delay, sleep=self.sleep)
                attempt += 1

        raise PrintablesUnavailable(
            f"Printables {operation_name} failed after {max_attempts} attempts: {last_error}"
        ) from last_error

    def _fetch_model_files(
        self, model_id: str, *, stop: Event | None = None
    ) -> list[dict[str, str]]:
        query = """query ModelFiles($id: ID!) {
          model: print(id: $id) {
            id
            name
            filesType
            stls { id name fileSize }
            gcodes { id name fileSize }
            slas { id name fileSize }
            otherFiles { id name fileSize }
          }
        }"""
        data = self._graphql_post(query, {"id": model_id}, "ModelFiles", stop=stop)
        model = data.get("model")
        if not model or not isinstance(model, Mapping):
            return []

        type_mapping = {
            "stls": "stl",
            "gcodes": "gcode",
            "slas": "sla",
            "otherFiles": "other",
        }
        results: list[dict[str, str]] = []
        for key, type_str in type_mapping.items():
            file_list = model.get(key)
            if isinstance(file_list, list):
                for item in file_list:
                    if (
                        isinstance(item, Mapping)
                        and item.get("id")
                        and item.get("name")
                    ):
                        results.append(
                            {
                                "id": str(item["id"]),
                                "name": str(item["name"]),
                                "type": type_str,
                            }
                        )
        return results

    def _get_download_link(
        self,
        file_id: str,
        model_id: str,
        file_type: str,
        *,
        stop: Event | None = None,
    ) -> str:
        query = """mutation GetDownloadLink($id: ID!, $modelId: ID!, $fileType: DownloadFileTypeEnum!, $source: DownloadSourceEnum!) {
          getDownloadLink(id: $id, printId: $modelId, fileType: $fileType, source: $source) {
            ok
            output {
              link
              count
              ttl
            }
          }
        }"""
        variables = {
            "id": file_id,
            "modelId": model_id,
            "fileType": file_type,
            "source": "model_detail",
        }
        data = self._graphql_post(query, variables, "GetDownloadLink", stop=stop)
        result = data.get("getDownloadLink")
        if isinstance(result, Mapping) and result.get("ok"):
            output = result.get("output")
            if isinstance(output, Mapping) and output.get("link"):
                return str(output["link"])
        raise PrintablesUnavailable(
            f"failed to obtain download link for file {file_id} of model {model_id}"
        )


def _create_deterministic_zip(
    file_paths: list[tuple[str, Path]], destination: Path
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_zip = destination.parent / f".{destination.name}.tmp"
    with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in sorted(file_paths, key=lambda x: x[0]):
            info = zipfile.ZipInfo(arcname, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with path.open("rb") as f:
                zf.writestr(info, f.read())
    temp_zip.replace(destination)
    return destination
