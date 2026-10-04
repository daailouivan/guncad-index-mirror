from __future__ import annotations

import base64
import logging
import re
import shutil
import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event
from urllib.parse import urljoin, urlsplit

import requests

from .cancellation import check_cancelled, wait_or_cancel
from .models import AcquisitionTransport, Release
from .paths import safe_component
from .progress import (
    ActivityPhase,
    ActivityUpdate,
    NullProgressReporter,
    ProgressReporter,
)
from .qbittorrent import QBitClient, QBitError
from .sessions import ThreadLocalSessionPool
from .settings import Settings
from .torrent import parse_torrent


class TorrentAcquisitionError(RuntimeError):
    """External BitTorrent acquisition failed."""


class TorrentAcquisitionProtocolError(TorrentAcquisitionError):
    """Torrent source returned malformed metadata."""


class TorrentAcquisitionUnavailable(TorrentAcquisitionError):
    """qBittorrent or the BitTorrent swarm is unavailable."""


class TorrentAcquisitionTimeout(TorrentAcquisitionError):
    """BitTorrent acquisition timed out before the payload was fully downloaded."""


@dataclass(frozen=True, slots=True)
class TorrentAcquisition:
    path: Path
    source_url: str | None = None


def extract_info_hash_from_magnet(magnet_uri: str) -> str | None:
    """Extract a 40-character lowercase hex BTIH from a magnet URI."""
    match_hex = re.search(r"xt=urn:btih:([0-9a-fA-F]{40})", magnet_uri)
    if match_hex:
        return match_hex.group(1).lower()

    match_b32 = re.search(r"xt=urn:btih:([2-7a-zA-Z]{32})", magnet_uri)
    if match_b32:
        try:
            raw = base64.b32decode(match_b32.group(1).upper())
            return raw.hex().lower()
        except Exception:
            return None
    return None


class TorrentAcquirer:
    def __init__(
        self,
        settings: Settings,
        client: QBitClient | None = None,
        session: requests.Session | ThreadLocalSessionPool | None = None,
        intake_category: str | None = None,
        intake_tag: str | None = None,
        download_timeout: float | None = None,
        poll_interval: float = 2.0,
        logger: logging.Logger | None = None,
        progress: ProgressReporter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.client = client
        self.session = session or ThreadLocalSessionPool()
        self.intake_category = intake_category or settings.torrent_intake_category
        self.intake_tag = intake_tag or settings.torrent_intake_tag
        self.download_timeout = (
            download_timeout
            if download_timeout is not None
            else settings.torrent_download_timeout
        )
        self.poll_interval = poll_interval
        self.logger = logger or logging.getLogger("guncad-mirror.torrent-acquirer")
        self.progress = progress or NullProgressReporter()
        self.sleep = sleep
        self.monotonic = monotonic

    def close(self) -> None:
        if hasattr(self.session, "close"):
            self.session.close()

    def acquire(
        self,
        release: Release,
        output_directory: Path,
        *,
        stop: Event | None = None,
    ) -> TorrentAcquisition:
        check_cancelled(stop)
        if self.client is None:
            raise TorrentAcquisitionUnavailable(
                "qBittorrent client is not configured or enabled for torrent acquisition"
            )

        self.progress.update_activity(
            ActivityUpdate(
                release=release,
                phase=ActivityPhase.TORRENT_SWARM,
                transport=AcquisitionTransport.TORRENT,
                total_bytes=release.size,
            )
        )

        source_url, info_hash = self._resolve_source(release, stop=stop)
        output_directory.mkdir(parents=True, exist_ok=True)
        qbit_save_path = self._qbit_save_path(output_directory)

        # Add to qBittorrent
        try:
            if source_url.startswith("magnet:"):
                self.client.add_url(
                    source_url,
                    save_path=qbit_save_path,
                    category=self.intake_category,
                    tag=self.intake_tag,
                )
            else:
                # Direct .torrent URL
                self._add_torrent_file(
                    source_url, output_directory, qbit_save_path, stop=stop
                )
        except QBitError as error:
            raise TorrentAcquisitionUnavailable(
                f"failed to add torrent to qBittorrent: {error}"
            ) from error

        self.client.force_start(info_hash)
        self.client.reannounce(info_hash)

        # Poll for completion
        deadline = self.monotonic() + self.download_timeout
        timed_out = True

        try:
            while self.monotonic() < deadline:
                check_cancelled(stop)
                torrent = self.client.torrent(info_hash)
                if torrent is not None:
                    self.progress.update_activity(
                        ActivityUpdate(
                            release=release,
                            phase=ActivityPhase.TORRENT_SWARM,
                            transport=AcquisitionTransport.TORRENT,
                            completed_bytes=torrent.total_size - torrent.amount_left,
                            total_bytes=torrent.total_size,
                        )
                    )
                    if torrent.progress == 1.0 and torrent.amount_left == 0:
                        timed_out = False
                        break
                wait_or_cancel(stop, self.poll_interval, sleep=self.sleep)
        finally:
            # Clean up intake entry in qBittorrent without deleting downloaded files
            try:
                self.client.delete(info_hash, delete_files=False)
            except Exception as error:
                self.logger.debug(
                    "Error removing intake torrent %s from qBittorrent: %s",
                    info_hash,
                    error,
                )

        if timed_out:
            check_cancelled(stop)
            raise TorrentAcquisitionTimeout(
                f"torrent acquisition timed out after {self.download_timeout:.0f}s for {release.id}"
            )

        payload_path = self._locate_payload(output_directory, release)
        return TorrentAcquisition(path=payload_path, source_url=source_url)

    def _resolve_source(
        self,
        release: Release,
        *,
        stop: Event | None = None,
    ) -> tuple[str, str]:
        """Return (source_url, info_hash)."""
        origin = release.raw.get("origin") if isinstance(release.raw, Mapping) else None
        links = origin.get("links") if isinstance(origin, Mapping) else None

        candidates: list[str] = []
        if isinstance(links, list):
            for item in links:
                if not isinstance(item, Mapping):
                    continue
                url = item.get("url")
                if not isinstance(url, str) or not url.strip():
                    continue
                name = str(item.get("name", "")).lower()
                is_magnet = url.startswith("magnet:")
                is_torrent = url.endswith(".torrent") or "torrent" in name
                if is_magnet or is_torrent or item.get("download") is True:
                    # Resolve relative URLs
                    if url.startswith("/"):
                        parsed_endpoint = urlsplit(self.settings.endpoint)
                        url = urljoin(
                            f"{parsed_endpoint.scheme}://{parsed_endpoint.netloc}", url
                        )
                    candidates.append(url)

        if release.url:
            candidates.append(release.url)

        for candidate in candidates:
            if candidate.startswith("magnet:"):
                btih = extract_info_hash_from_magnet(candidate)
                if btih:
                    return candidate, btih
            elif candidate.endswith(".torrent") or "/torrents/" in candidate:
                btih = self._probe_torrent_url(candidate, stop=stop)
                if btih:
                    return candidate, btih

        raise TorrentAcquisitionError(
            f"release {release.id} has no valid magnet URI or .torrent link"
        )

    def _probe_torrent_url(self, url: str, *, stop: Event | None = None) -> str | None:
        check_cancelled(stop)
        try:
            active_session = self.session or requests.Session()
            response = active_session.get(url, timeout=(10, 30))
            if response.status_code == 200:
                parsed = parse_torrent(response.content)
                return parsed.info_hash
        except Exception as error:
            self.logger.debug("Failed to probe .torrent URL %s: %s", url, error)
        return None

    def _add_torrent_file(
        self,
        url: str,
        output_directory: Path,
        qbit_save_path: str,
        *,
        stop: Event | None = None,
    ) -> None:
        check_cancelled(stop)
        active_session = self.session or requests.Session()
        response = active_session.get(url, timeout=(10, 30))
        response.raise_for_status()

        temp_torrent = output_directory / ".intake.torrent"
        temp_torrent.write_bytes(response.content)
        try:
            if self.client is not None:
                self.client.add(
                    temp_torrent,
                    save_path=qbit_save_path,
                    category=self.intake_category,
                    tag=self.intake_tag,
                )
        finally:
            temp_torrent.unlink(missing_ok=True)

    def _qbit_save_path(self, output_directory: Path) -> str:
        data_dir_resolved = self.settings.data_dir.resolve()
        try:
            relative = output_directory.resolve().relative_to(data_dir_resolved)
            return str(
                PurePosixPath(self.settings.qbittorrent_data_dir.as_posix()).joinpath(
                    *relative.parts
                )
            )
        except ValueError:
            # Fallback for temporary / test directories outside data_dir
            return str(output_directory)

    def _locate_payload(self, output_directory: Path, release: Release) -> Path:
        """Find the completed file in output_directory, packaging directories into a ZIP if needed."""
        # Find non-hidden files and directories
        items = [p for p in output_directory.iterdir() if not p.name.startswith(".")]
        if not items:
            raise TorrentAcquisitionError(
                f"no downloaded payload files found in {output_directory} for {release.id}"
            )

        # Single file
        if len(items) == 1 and items[0].is_file():
            return items[0]

        # Single directory containing files
        if len(items) == 1 and items[0].is_dir():
            target_dir = items[0]
            entries: list[tuple[str, Path]] = []
            for file_path in target_dir.rglob("*"):
                if file_path.is_file() and not file_path.name.startswith("."):
                    rel_name = file_path.relative_to(target_dir).as_posix()
                    entries.append((rel_name, file_path))

            zip_name = f"{safe_component(release.name, fallback=release.id)}.zip"
            zip_dest = output_directory / zip_name
            _create_deterministic_zip(entries, zip_dest)
            shutil.rmtree(target_dir, ignore_errors=True)
            return zip_dest

        # Multiple files directly in output_directory
        file_entries: list[tuple[str, Path]] = []
        for file_path in items:
            if file_path.is_file():
                file_entries.append((file_path.name, file_path))

        zip_name = f"{safe_component(release.name, fallback=release.id)}.zip"
        zip_dest = output_directory / zip_name
        _create_deterministic_zip(file_entries, zip_dest)
        for _, path in file_entries:
            path.unlink(missing_ok=True)
        return zip_dest


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
