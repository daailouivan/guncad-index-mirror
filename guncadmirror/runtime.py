from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Event

from .cancellation import AcquisitionCancelled
from .index_client import IndexClient
from .index_publisher import IndexPublisherClient
from .lbry import LbryAcquirer, LbryClient
from .odysee import OdyseeAcquirer
from .pipeline import CycleResult, MirrorPipeline
from .publication import PublicationCycleResult, PublicationScheduler
from .publisher import OutboxPublisher
from .qbittorrent import QBitClient
from .seeding import SeedingCycleResult, SeedingScheduler
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
    seeding: SeedingScheduler | None = None
    publication: PublicationScheduler | None = None
    lbry_ready: bool = False
    seeding_degraded: bool = False
    publication_paused: bool = False

    def start(self, stop: Event | None = None) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.stats.start()
        if self.settings.enable_webui:
            start_webui(self.stats)

    def _ensure_lbry(self, stop: Event | None) -> None:
        if self.lbry_ready:
            return
        self.stats.set_state("Waiting for LBRY")
        self.lbry.wait_until_ready(self.settings.lbry_startup_timeout, stop=stop)
        self.lbry_ready = True
        logging.getLogger("guncad-mirror").info("LBRY daemon is ready")

    def run_cycle(self, stop: Event | None = None) -> CycleResult:
        seeding = self._run_seeding(stop)
        publication = (
            self._run_publication(stop)
            if seeding.error_code is None
            else PublicationCycleResult()
        )
        self._ensure_lbry(stop)
        self.stats.set_state("Enumerating Index releases")
        result = self.pipeline.run_cycle(stop)
        if not seeding.paused:
            seeding += self._run_seeding(stop)
        if not publication.paused and seeding.error_code is None:
            publication += self._run_publication(stop)
        self.seeding_degraded = seeding.error_code is not None
        self.publication_paused = publication.paused
        summary = (
            f"Cycle complete: {result.discovered} discovered, {result.ready} ready, "
            f"{result.skipped} skipped, {result.failed} failed, "
            f"{result.stopped} stopped"
        )
        self.stats.set_state(summary)
        self.stats.log(summary, stdout=True)
        return result

    def _run_seeding(self, stop: Event | None) -> SeedingCycleResult:
        if self.seeding is None:
            return SeedingCycleResult()
        self.stats.set_state("Reconciling qBittorrent seed readiness")
        result = self.seeding.run(stop)
        self.seeding_degraded = result.error_code is not None
        if result.attempted:
            self.stats.log(
                "qBittorrent seeding pass: "
                f"{result.attempted} attempted, {result.green} green, "
                f"{result.retrying} retrying, {result.blocked} blocked",
                stdout=True,
            )
        return result

    def _run_publication(
        self,
        stop: Event | None,
    ) -> PublicationCycleResult:
        if self.publication is None:
            return PublicationCycleResult()
        self.stats.set_state("Publishing staged torrents to the Index")
        result = self.publication.run(stop)
        self.publication_paused = result.paused
        if result.attempted:
            self.stats.log(
                "Index publication pass: "
                f"{result.attempted} attempted, {result.published} published, "
                f"{result.duplicates} duplicates, {result.rejected} rejected, "
                f"{result.conflicts} conflicts, {result.retrying} retrying",
                stdout=True,
            )
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
            if self.publication is not None:
                publication_delay = self.publication.next_delay()
                if publication_delay is not None:
                    minimum = (
                        self.settings.cycle_error_interval
                        if self.publication_paused
                        else 1
                    )
                    delay = min(delay, max(publication_delay, minimum))
            if self.seeding is not None:
                seeding_delay = self.seeding.next_delay()
                if seeding_delay is not None:
                    minimum = (
                        self.settings.cycle_error_interval
                        if self.seeding_degraded
                        else 1
                    )
                    delay = min(delay, max(seeding_delay, minimum))
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
        if self.seeding is not None:
            cleanups.append(("qBittorrent session", self.seeding.close))
        if self.publication is not None:
            cleanups.append(("Index publication session", self.publication.close))
        for description, close in cleanups:
            try:
                close()
            except Exception:
                logger.exception("Failed to close %s", description)


def build_runtime(settings: Settings) -> Runtime:
    store = JobStore(settings.state_path)
    recovered_seeding = store.recover_interrupted_seeding()
    if recovered_seeding:
        logging.getLogger("guncad-mirror").warning(
            "Recovered %d interrupted qBittorrent injection attempts",
            recovered_seeding,
        )
    recovered_publications = store.recover_interrupted_publications()
    if recovered_publications:
        logging.getLogger("guncad-mirror").warning(
            "Recovered %d interrupted Index publication attempts",
            recovered_publications,
        )
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
    seeding = (
        SeedingScheduler(
            settings,
            store,
            QBitClient(
                settings.qbittorrent_url,
                timeout=settings.qbittorrent_timeout,
                api_key=settings.qbittorrent_api_key,
                username=settings.qbittorrent_username,
                password=settings.qbittorrent_password,
            ),
            record_event=stats.log,
        )
        if settings.qbittorrent_enabled
        else None
    )
    publication = (
        PublicationScheduler(
            settings,
            store,
            IndexPublisherClient(
                settings.publish_url,
                settings.publish_token,
                timeout=settings.publish_timeout,
            ),
            record_event=stats.log,
        )
        if settings.publish_enabled
        else None
    )
    return Runtime(
        settings=settings,
        lbry=lbry,
        pipeline=pipeline,
        stats=stats,
        odysee=odysee,
        seeding=seeding,
        publication=publication,
    )
