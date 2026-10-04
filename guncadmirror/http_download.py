from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event

import requests

from .cancellation import check_cancelled, wait_or_cancel
from .index_client import USER_AGENT
from .sessions import ThreadLocalSessionPool


class DownloadError(RuntimeError):
    """An HTTP download failed."""


class DownloadCancelled(DownloadError):
    """The download was cancelled."""


def download_url_to_file(
    url: str,
    destination: Path,
    *,
    headers: Mapping[str, str] | None = None,
    session: requests.Session | ThreadLocalSessionPool | None = None,
    chunk_size: int = 1024 * 1024,
    attempts: int = 5,
    backoff: float = 2.0,
    read_timeout: float = 60.0,
    stop: Event | None = None,
    progress: Callable[[int, int | None], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> int:
    check_cancelled(stop)
    logger = logger or logging.getLogger("guncad-mirror.download")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = destination.parent

    active_session = session or requests.Session()
    last_error: Exception | None = None
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)

    for attempt in range(1, attempts + 1):
        check_cancelled(stop)
        try:
            with active_session.get(
                url,
                stream=True,
                headers=req_headers,
                timeout=(10, read_timeout),
            ) as response:
                response.raise_for_status()
                total_header = response.headers.get("Content-Length")
                total_bytes = (
                    int(total_header) if total_header and total_header.isdigit() else None
                )
                bytes_downloaded = 0

                with NamedTemporaryFile(
                    dir=temp_dir, prefix=f".{destination.name}.", delete=False
                ) as temp_file:
                    temp_path = Path(temp_file.name)
                    try:
                        for chunk in response.iter_content(chunk_size=chunk_size):
                            check_cancelled(stop)
                            if not chunk:
                                continue
                            temp_file.write(chunk)
                            bytes_downloaded += len(chunk)
                            if progress is not None:
                                progress(bytes_downloaded, total_bytes)

                        temp_file.flush()
                        os.fsync(temp_file.fileno())
                    except Exception:
                        temp_path.unlink(missing_ok=True)
                        raise

                temp_path.replace(destination)
                return bytes_downloaded

        except (requests.RequestException, OSError) as error:
            last_error = error
            if attempt == attempts:
                break
            delay = backoff * (2 ** (attempt - 1))
            logger.warning(
                "Download %s attempt %d/%d failed: %s; retrying in %.1fs",
                url,
                attempt,
                attempts,
                error,
                delay,
            )
            wait_or_cancel(stop, delay, sleep=sleep)

    raise DownloadError(
        f"failed to download {url} after {attempts} attempts: {last_error}"
    ) from last_error
