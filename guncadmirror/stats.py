from __future__ import annotations

import logging
import os
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

import psutil

from .models import Release
from .progress import ActivityUpdate
from .settings import Settings
from .state import JobStore
from .tracker_policy import TrackerPolicyStatus


class StatsCollector:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        *,
        cheap_interval: float = 1,
        disk_interval: float = 30,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.store = store
        self.cheap_interval = cheap_interval
        self.disk_interval = disk_interval
        self.monotonic = monotonic
        self.events: deque[str] = deque(maxlen=512)
        self._snapshot: dict[str, Any] = {
            "version": os.getenv("GUNCAD_COMMIT_REF", "Unknown"),
            "mirror_state": "Starting",
            "mirror_api_endpoint": settings.endpoint,
            "mirror_api_max_pages": settings.api_max_pages,
            "mirror_max_releases_per_run": settings.max_releases_per_run,
            "mirror_lbry_url": settings.lbry_url,
            "mirror_lbry_concurrency": settings.lbry_concurrency,
            "mirror_odysee_concurrency": settings.odysee_concurrency,
            "mirror_printables_concurrency": settings.printables_concurrency,
            "mirror_github_concurrency": settings.github_concurrency,
            "mirror_http_concurrency": settings.http_concurrency,
            "mirror_torrent_concurrency": settings.torrent_concurrency,
            "mirror_torrent_intake_category": settings.torrent_intake_category,
            "mirror_torrent_intake_tag": settings.torrent_intake_tag,
            "mirror_torrent_download_timeout": settings.torrent_download_timeout,
            "mirror_finalize_concurrency": settings.finalize_concurrency,
            "mirror_enable_webui": settings.enable_webui,
            "mirror_blacklisted_handles": settings.blacklisted_handles,
            "mirror_release_max_size": settings.max_release_size,
            "mirror_min_free_space": settings.min_free_space,
            "mirror_loop_interval": settings.loop_interval,
            "mirror_cycle_error_interval": settings.cycle_error_interval,
            "mirror_download_timeout": settings.download_timeout,
            "mirror_torrent_piece_length": settings.torrent_piece_length,
            "mirror_torrent_trackers": settings.torrent_trackers,
            "mirror_tracker_policy_url": settings.effective_tracker_policy_url,
            "mirror_tracker_policy_timeout": settings.tracker_policy_timeout,
            "mirror_qbittorrent_enabled": settings.qbittorrent_enabled,
            "mirror_qbittorrent_url": settings.qbittorrent_url,
            "mirror_qbittorrent_data_dir": str(settings.qbittorrent_data_dir),
            "mirror_qbittorrent_timeout": settings.qbittorrent_timeout,
            "mirror_qbittorrent_ready_timeout": settings.qbittorrent_ready_timeout,
            "mirror_qbittorrent_recheck_interval": (
                settings.qbittorrent_recheck_interval
            ),
            "mirror_qbittorrent_category": settings.qbittorrent_category,
            "mirror_qbittorrent_tag": settings.qbittorrent_tag,
            "mirror_publish_enabled": settings.publish_enabled,
            "mirror_publish_url": settings.publish_url,
            "mirror_publish_concurrency": settings.publish_concurrency,
            "mirror_publish_timeout": settings.publish_timeout,
            "mirror_data_dir": str(settings.data_dir),
            "mirror_releases_dir": str(settings.releases_dir),
            "mirror_outbox_dir": str(settings.outbox_dir),
            "disk_space_used": 0,
            "job_counts": {},
            "seeding_counts": {},
            "publication_counts": {},
            "platform_breakdown": {},
            "platform_totals": {},
            "source_file_counts": {},
            "source_staged_counts": {},
            "known_jobs": 0,
            "activity": None,
            "activities": [],
            "tracker_policy": {
                "enabled": bool(
                    settings.qbittorrent_enabled
                    and settings.effective_tracker_policy_url
                ),
                "endpoint": settings.effective_tracker_policy_url,
                "source": (
                    "uninitialized"
                    if settings.qbittorrent_enabled
                    and settings.effective_tracker_policy_url
                    else "disabled"
                ),
                "removals_authoritative": not bool(
                    settings.qbittorrent_enabled
                    and settings.effective_tracker_policy_url
                ),
                "desired_trackers": settings.torrent_trackers,
                "enabled_index_trackers": 0,
                "blacklisted_trackers": 0,
                "etag": None,
                "cached_at": None,
                "last_checked_at": None,
                "last_success_at": None,
                "error_code": None,
                "error": None,
            },
            "github_token_configured": bool(
                self.store.get_setting("github_token") or settings.github_token
            ),
            "github_token_masked": (
                f"{(self.store.get_setting('github_token') or settings.github_token)[:4]}...{(self.store.get_setting('github_token') or settings.github_token)[-4:]}"
                if len(self.store.get_setting("github_token") or settings.github_token)
                >= 8
                else (
                    "Configured"
                    if (self.store.get_setting("github_token") or settings.github_token)
                    else ""
                )
            ),
        }
        self._activities: dict[tuple[str, str], dict[str, Any]] = {}
        self._activity_rates: dict[
            tuple[str, str],
            tuple[tuple[str, str | None], int | None, float],
        ] = {}
        self._lock = Lock()
        self._stop = Event()
        self._threads: list[Thread] = []

    def set_state(self, state: str) -> None:
        with self._lock:
            self._snapshot["mirror_state"] = state

    def update_tracker_policy(self, status: TrackerPolicyStatus) -> None:
        with self._lock:
            self._snapshot["tracker_policy"] = {
                "enabled": status.enabled,
                "endpoint": status.endpoint,
                "source": status.source,
                "removals_authoritative": status.removals_authoritative,
                "desired_trackers": status.desired_trackers,
                "enabled_index_trackers": status.enabled_index_trackers,
                "blacklisted_trackers": status.blacklisted_trackers,
                "etag": status.etag,
                "cached_at": status.cached_at,
                "last_checked_at": status.last_checked_at,
                "last_success_at": status.last_success_at,
                "error_code": status.error_code,
                "error": status.error,
            }

    def update_activity(self, update: ActivityUpdate) -> None:
        transport = update.transport.value if update.transport is not None else None
        job_key = (update.release.id, update.release.sd_hash)
        phase_key = (update.phase.value, transport)
        now = self.monotonic()
        with self._lock:
            previous = self._activity_rates.get(job_key)
            if previous is None or previous[0] != phase_key:
                base_bytes = update.completed_bytes
                base_time = now
            else:
                _previous_phase, base_bytes, base_time = previous
                if base_bytes is None and update.completed_bytes is not None:
                    base_bytes = update.completed_bytes
                    base_time = now

            rate = None
            if (
                update.completed_bytes is not None
                and base_bytes is not None
                and update.completed_bytes >= base_bytes
                and now > base_time
            ):
                rate = (update.completed_bytes - base_bytes) / (now - base_time)

            self._activity_rates[job_key] = (phase_key, base_bytes, base_time)
            activity = {
                "release_id": update.release.id,
                "sd_hash": update.release.sd_hash,
                "release_name": update.release.name,
                "channel_handle": update.release.channel_handle,
                "phase": update.phase.value,
                "transport": transport,
                "completed_bytes": update.completed_bytes,
                "total_bytes": update.total_bytes,
                "bytes_per_second": rate,
                "blobs_remaining": update.blobs_remaining,
            }
            self._activities[job_key] = activity
            self._snapshot["activity"] = activity
            self._snapshot["activities"] = list(self._activities.values())

    def clear_activity(self, release: Release | None = None) -> None:
        with self._lock:
            if release is None:
                self._activities.clear()
                self._activity_rates.clear()
            else:
                job_key = (release.id, release.sd_hash)
                self._activities.pop(job_key, None)
                self._activity_rates.pop(job_key, None)
            activities = list(self._activities.values())
            self._snapshot["activities"] = activities
            self._snapshot["activity"] = activities[-1] if activities else None

    def log(self, message: str, *, stdout: bool = False) -> None:
        if stdout:
            logging.getLogger("guncad-mirror").info(message)
        with self._lock:
            self.events.append(f"[{datetime.now()}] {message}")

    def collect(self) -> None:
        counts = self.store.counts()
        seeding_counts = self.store.seeding_counts()
        publication_counts = self.store.publication_counts()
        breakdown = self.store.platform_breakdown()
        totals = {
            "platform": "all",
            "display_name": "All Sources",
            "total": sum(s["total"] for s in breakdown.values()),
            "pending": sum(s["pending"] for s in breakdown.values()),
            "acquiring": sum(s["acquiring"] for s in breakdown.values()),
            "verified": sum(s["verified"] for s in breakdown.values()),
            "awaiting_index": sum(s["awaiting_index"] for s in breakdown.values()),
            "seeding_green": sum(s["seeding_green"] for s in breakdown.values()),
            "published": sum(s["published"] for s in breakdown.values()),
            "failed": sum(s["failed"] for s in breakdown.values()),
            "excluded": sum(s["excluded"] for s in breakdown.values()),
            "staged_bytes": sum(s["staged_bytes"] for s in breakdown.values()),
            "total_bytes": sum(s["total_bytes"] for s in breakdown.values()),
        }
        values = {
            "psutil_cpu": psutil.cpu_percent(interval=None),
            "psutil_mem": psutil.virtual_memory().percent,
            "psutil_net": psutil.net_io_counters(),
            "psutil_disk": psutil.disk_usage(self.settings.data_dir),
            "job_counts": counts,
            "seeding_counts": seeding_counts,
            "publication_counts": publication_counts,
            "platform_breakdown": breakdown,
            "platform_totals": totals,
            "source_file_counts": {plat: s["total"] for plat, s in breakdown.items()},
            "source_staged_counts": {
                plat: s["awaiting_index"] for plat, s in breakdown.items()
            },
            "known_jobs": sum(counts.values()),
            "github_token_configured": bool(
                self.store.get_setting("github_token") or self.settings.github_token
            ),
            "github_token_masked": (
                f"{(self.store.get_setting('github_token') or self.settings.github_token)[:4]}...{(self.store.get_setting('github_token') or self.settings.github_token)[-4:]}"
                if len(
                    self.store.get_setting("github_token") or self.settings.github_token
                )
                >= 8
                else (
                    "Configured"
                    if (
                        self.store.get_setting("github_token")
                        or self.settings.github_token
                    )
                    else ""
                )
            ),
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
