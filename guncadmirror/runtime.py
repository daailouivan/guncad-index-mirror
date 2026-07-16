from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Event

from .cancellation import AcquisitionCancelled
from .index_client import IndexClient
from .lbry import LbryAcquirer, LbryClient
from .odysee import OdyseeAcquirer
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
    odysee: OdyseeAcquirer | None = None

    def start(self, stop: Event | None = None) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.stats.start()
        if self.settings.enable_webui:
            start_webui(self.stats)
        self.stats.set_state("Waiting for LBRY")
        self.lbry.wait_until_ready(self.settings.lbry_startup_timeout, stop=stop)
        logging.getLogger("guncad-mirror").info("LBRY daemon is ready")

    def run_cycle(self, stop: Event | None = None) -> CycleResult:
        self.stats.set_state("Enumerating Index releases")
        result = self.pipeline.run_cycle(stop)
        summary = (
            f"Cycle complete: {result.discovered} discovered, {result.ready} ready, "
            f"{result.skipped} skipped, {result.failed} failed, "
            f"{result.stopped} stopped"
        )
        self.stats.set_state(summary)
        self.stats.log(summary, stdout=True)
        return result

    def run_forever(self, stop: Event) -> None:
        while not stop.is_set():
            delay = self.settings.loop_interval
            try:
                self.run_cycle(stop)
            except AcquisitionCancelled:
                self.stats.log("Mirror stop requested during Index enumeration")
                return
            except Exception:
                logging.getLogger("guncad-mirror").exception("Mirror cycle failed")
                delay = self.settings.cycle_error_interval
                self.stats.log(
                    f"Mirror cycle failed; retrying in {delay:.0f}s; "
                    "see application log"
                )
            self.stats.set_state(f"Sleeping for {delay:.0f}s")
            stop.wait(delay)

    def stop(self) -> None:
        logger = logging.getLogger("guncad-mirror")
        cleanups = [
            ("statistics collector", self.stats.stop),
            ("Index HTTP session", self.pipeline.index_client.close),
            ("LBRY HTTP session", self.lbry.close),
        ]
        if self.odysee is not None:
            cleanups.append(("Odysee HTTP session", self.odysee.close))
        for description, close in cleanups:
            try:
                close()
            except Exception:
                logger.exception("Failed to close %s", description)


def build_runtime(settings: Settings) -> Runtime:
    store = JobStore(settings.state_path)
    stats = StatsCollector(settings, store)
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
        progress=stats,
    )
    odysee = (
        OdyseeAcquirer(
            settings.odysee_proxy_url,
            data_root=settings.data_dir,
            attempts=settings.retry_attempts,
            backoff=settings.retry_backoff,
            read_timeout=min(settings.download_timeout, 60),
            progress=stats,
        )
        if settings.odysee_fallback
        else None
    )
    publisher = OutboxPublisher(settings.outbox_dir)
    pipeline = MirrorPipeline(
        settings,
        index_client,
        acquirer,
        store,
        publisher,
        fallback_acquirer=odysee,
        progress=stats,
        record_event=stats.log,
    )
    return Runtime(
        settings=settings,
        lbry=lbry,
        pipeline=pipeline,
        stats=stats,
        odysee=odysee,
    )
