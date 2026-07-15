from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

import psutil

from .settings import Settings
from .state import JobStore


class StatsCollector:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        *,
        cheap_interval: float = 1,
        disk_interval: float = 30,
    ):
        self.settings = settings
        self.store = store
        self.cheap_interval = cheap_interval
        self.disk_interval = disk_interval
        self.events: deque[str] = deque(maxlen=512)
        self._snapshot: dict[str, Any] = {
            "version": os.getenv("GUNCAD_COMMIT_REF", "Unknown"),
            "mirror_state": "Starting",
            "mirror_api_endpoint": settings.endpoint,
            "mirror_api_max_pages": settings.api_max_pages,
            "mirror_max_releases_per_run": settings.max_releases_per_run,
            "mirror_lbry_url": settings.lbry_url,
            "mirror_enable_webui": settings.enable_webui,
            "mirror_blacklisted_handles": settings.blacklisted_handles,
            "mirror_release_max_size": settings.max_release_size,
            "mirror_min_free_space": settings.min_free_space,
            "mirror_loop_interval": settings.loop_interval,
            "mirror_download_timeout": settings.download_timeout,
            "mirror_torrent_piece_length": settings.torrent_piece_length,
            "mirror_torrent_trackers": settings.torrent_trackers,
            "mirror_data_dir": str(settings.data_dir),
            "mirror_releases_dir": str(settings.releases_dir),
            "mirror_outbox_dir": str(settings.outbox_dir),
            "disk_space_used": 0,
            "job_counts": {},
            "known_jobs": 0,
        }
        self._lock = Lock()
        self._stop = Event()
        self._threads: list[Thread] = []

    def set_state(self, state: str) -> None:
        with self._lock:
            self._snapshot["mirror_state"] = state

    def log(self, message: str, *, stdout: bool = False) -> None:
        if stdout:
            logging.getLogger("guncad-mirror").info(message)
        self.events.append(f"[{datetime.now()}] {message}")

    def collect(self) -> None:
        counts = self.store.counts()
        values = {
            "psutil_cpu": psutil.cpu_percent(interval=None),
            "psutil_mem": psutil.virtual_memory().percent,
            "psutil_net": psutil.net_io_counters(),
            "psutil_disk": psutil.disk_usage(self.settings.data_dir),
            "job_counts": counts,
            "known_jobs": sum(counts.values()),
        }
        with self._lock:
            self._snapshot.update(values)

    def collect_disk(self) -> None:
        value = directory_size(self.settings.data_dir)
        with self._lock:
            self._snapshot["disk_space_used"] = value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._snapshot)
        result["extralog"] = list(self.events)
        return result

    def start(self) -> None:
        self.collect()
        for target, interval, name in (
            (self.collect, self.cheap_interval, "mirror-stats"),
            (self.collect_disk, self.disk_interval, "mirror-disk-stats"),
        ):
            thread = Thread(
                target=self._run_collector,
                args=(target, interval),
                name=name,
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=1)

    def _run_collector(self, target: Any, interval: float) -> None:
        logger = logging.getLogger("guncad-mirror.stats")
        while not self._stop.wait(interval):
            try:
                target()
            except Exception:
                logger.exception("Statistics collection failed")


def directory_size(path: Path) -> int:
    total = 0
    for directory, _, filenames in os.walk(path):
        for filename in filenames:
            try:
                total += (Path(directory) / filename).stat().st_size
            except FileNotFoundError:
                continue
    return total
