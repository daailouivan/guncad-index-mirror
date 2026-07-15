from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Event

from .index_client import IndexClient
from .lbry import LbryAcquirer, LbryClient
from .pipeline import CycleResult, MirrorPipeline
from .publisher import OutboxPublisher
from .settings import Settings
from .state import JobStore
from .stats import StatsCollector
from .webui import start as start_webui


@dataclass(slots=True)
class Runtime:
    settings: Settings
    lbry: LbryClient
    pipeline: MirrorPipeline
    stats: StatsCollector

    def start(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.stats.start()
        if self.settings.enable_webui:
            start_webui(self.stats)
        self.stats.set_state("Waiting for LBRY")
        self.lbry.wait_until_ready(self.settings.lbry_startup_timeout)
        self.stats.log("LBRY daemon is ready", stdout=True)

    def run_cycle(self) -> CycleResult:
        self.stats.set_state("Enumerating Index releases")
        result = self.pipeline.run_cycle()
        summary = (
            f"Cycle complete: {result.discovered} discovered, {result.ready} ready, "
            f"{result.skipped} skipped, {result.failed} failed"
        )
        self.stats.set_state(summary)
        self.stats.log(summary, stdout=True)
        return result

    def run_forever(self, stop: Event) -> None:
        while not stop.is_set():
            try:
                self.run_cycle()
            except Exception:
                logging.getLogger("guncad-mirror").exception("Mirror cycle failed")
                self.stats.log("Mirror cycle failed; see application log")
            self.stats.set_state(f"Sleeping for {self.settings.loop_interval:.0f}s")
            stop.wait(self.settings.loop_interval)

    def stop(self) -> None:
        self.stats.stop()


def build_runtime(settings: Settings) -> Runtime:
    store = JobStore(settings.state_path)
    lbry = LbryClient(
        settings.lbry_url,
        attempts=settings.retry_attempts,
        backoff=settings.retry_backoff,
    )
    index_client = IndexClient(
        settings.endpoint,
        max_pages=settings.api_max_pages,
        max_releases=settings.max_releases_per_run,
        attempts=settings.retry_attempts,
        backoff=settings.retry_backoff,
    )
    acquirer = LbryAcquirer(
        lbry,
        data_root=settings.data_dir,
        download_timeout=settings.download_timeout,
        poll_interval=settings.download_poll_interval,
    )
    publisher = OutboxPublisher(settings.outbox_dir)
    pipeline = MirrorPipeline(settings, index_client, acquirer, store, publisher)
    stats = StatsCollector(settings, store)
    return Runtime(settings=settings, lbry=lbry, pipeline=pipeline, stats=stats)
