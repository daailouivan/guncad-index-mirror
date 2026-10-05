from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import requests

from guncadmirror.audit import (
    _acquisition_fields,
    _is_safe_http_url,
    _safe_path,
    _validate_magnet,
    _validate_seeding,
)
from guncadmirror.cancellation import AcquisitionCancelled
from guncadmirror.github import GitHubAcquirer, GitHubProtocolError
from guncadmirror.http_acquirer import HttpAcquirer, parse_content_disposition_filename
from guncadmirror.http_download import DownloadError, download_url_to_file
from guncadmirror.index_client import IndexClient
from guncadmirror.index_publisher import (
    REQUEST_SCHEMA,
    PublicationPaused,
    _absolute_http_url,
    _canonical_magnet,
    _mapping,
    _retry_after,
)
from guncadmirror.lbry import LbryAcquirer, LbryError, LbryProtocolError, _is_ready
from guncadmirror.migration import (
    MigrationConfig,
    V1MigrationRunner,
    decode_hex_string,
    load_bootstrap_index,
    main,
    read_v1_lbrynet_db,
    read_v1_meta_json,
    synthesize_v2_payload,
)
from guncadmirror.models import (
    AcquisitionEvidence,
    AcquisitionTransport,
    JobState,
    PublicationState,
    SeedingState,
    TorrentArtifact,
)
from guncadmirror.odysee import _retry_after_seconds
from guncadmirror.pipeline import (
    MirrorPipeline,
    _valid_acquisition_document,
)
from guncadmirror.printables import (
    PrintablesAcquirer,
    PrintablesProtocolError,
    PrintablesUnavailable,
)
from guncadmirror.publication import (
    PublicationPreparationError,
    PublicationScheduler,
    prepare_submission,
)
from guncadmirror.publisher import OutboxPublisher
from guncadmirror.qbittorrent import (
    QBitError,
)
from guncadmirror.runtime import Runtime, build_runtime
from guncadmirror.seeding import SeedingScheduler, prepare_seed
from guncadmirror.settings import ConfigurationError, Settings
from guncadmirror.state import (
    JobStore,
    _archive_fields_from_json,
    _platform_from_json,
    _release_size_from_json,
    _release_slug,
)
from guncadmirror.stats import StatsCollector
from guncadmirror.torrent_acquirer import (
    TorrentAcquirer,
    TorrentAcquisitionError,
    TorrentAcquisitionUnavailable,
    extract_info_hash_from_magnet,
)
from guncadmirror.webui import create_app, humanize_bytes, humanize_seconds
from tests.helpers import FakeResponse, QueueSession, make_release, release_payload


def _platform_release(platform: str, release_id: str, **extra: object):
    raw = release_payload(b"payload", release_id=release_id, sd_hash="b" * 96)
    raw["id"] = release_id
    raw["origin"]["platform"] = platform
    raw["origin"]["external_id"] = extra.get("external_id", release_id)
    raw["origin"]["extra"] = {}
    if "links" in extra:
        raw["origin"]["links"] = extra["links"]
    if "url" in extra:
        raw["origin"]["links"] = [{"url": extra["url"]}]
    if extra.get("size") is None and "drop_size" in extra:
        raw["origin"]["size"] = None
    return raw


class StateGapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = 1_000.0
        self.store = JobStore(self.root / "state.sqlite3", clock=lambda: self.now)

    def _release(self, index: int):
        return make_release(
            release_id=f"{index:040x}",
            sd_hash=f"{index:096x}",
            name=f"Item {index}",
        )

    def test_category_filters_limits_and_nolock_reads(self) -> None:
        releases = [self._release(index) for index in range(1, 9)]
        for release in releases:
            self.store.register(release)

        self.store.start_attempt(releases[0])
        payload = self.root / "missing-payload.bin"
        verified = self.store.mark_verified(
            releases[1],
            file_path=payload,
            sha384="c" * 96,
            sha256="d" * 64,
        )
        self.assertEqual(verified.payload_size, releases[1].size)

        torrent = TorrentArtifact(
            file_path=payload,
            torrent_path=self.root / "missing.torrent",
            piece_length=16,
            piece_count=1,
            info_hash="e" * 40,
            torrent_sha256="f" * 64,
            magnet_uri="magnet:?xt=urn:btih:" + "e" * 40,
            trackers=(),
        )
        self.store.mark_awaiting_index(releases[2], torrent)
        self.store.mark_excluded(releases[3], "policy")
        self.store.start_attempt(releases[4])
        self.store.mark_failed(releases[4], RuntimeError("boom"), retry_backoff=1)
        self.store._update(
            releases[2],
            publication_state=PublicationState.PUBLISHED,
            seeding_state=SeedingState.GREEN,
        )
        self.store._update(releases[5], publication_state=PublicationState.PUBLISHING)
        self.store._update(releases[6], publication_state=PublicationState.RETRYING)
        self.store._update(releases[7], publication_state=PublicationState.REJECTED)
        self.store._update(releases[0], seeding_state=SeedingState.INJECTING)
        self.store._update(releases[1], seeding_state=SeedingState.BLOCKED)

        cases = (
            ("pipeline", "acquiring", releases[0].id),
            ("pipeline", "verified", releases[1].id),
            ("pipeline", "staged", releases[2].id),
            ("pipeline", "excluded", releases[3].id),
            ("publication", "published", releases[2].id),
            ("publication", "publishing", releases[5].id),
            ("publication", "retrying", releases[6].id),
            ("publication", "rejected", releases[7].id),
            ("publication", "pending", releases[0].id),
            ("seeding", "green", releases[2].id),
            ("seeding", "injecting", releases[0].id),
            ("seeding", "blocked", releases[1].id),
            ("seeding", "pending", releases[3].id),
            ("source", "published", releases[2].id),
            ("source", "green", releases[2].id),
            ("source", "inflight", releases[0].id),
            ("source", "failed", releases[4].id),
            ("source", "staged", releases[2].id),
        )
        for section, category, release_id in cases:
            with self.subTest(section=section, category=category):
                entries, total = self.store.get_category_entries(section, category)
                self.assertGreaterEqual(total, 1)
                self.assertIn(release_id, {entry["release_id"] for entry in entries})

        duplicate = make_release(
            release_id=f"{9:040x}",
            sd_hash=f"{9:096x}",
            name="Dup",
        )
        self.store.register(duplicate)
        self.store.mark_awaiting_index(duplicate, torrent)
        self.store._update(duplicate, publication_state=PublicationState.DUPLICATE)
        entries, total = self.store.get_category_entries("publication", "duplicates")
        self.assertEqual(total, 1)
        entries, total = self.store.get_category_entries("publication", "conflicts")
        self.assertEqual(total, 0)
        self.store._update(duplicate, publication_state=PublicationState.CONFLICT)
        entries, total = self.store.get_category_entries("publication", "conflict")
        self.assertEqual(total, 1)
        self.store._update(releases[6], seeding_state=SeedingState.RETRYING)
        entries, total = self.store.get_category_entries("seeding", "retrying")
        self.assertGreaterEqual(total, 1)
        entries, total = self.store.get_category_entries("source", "in-flight")
        self.assertGreaterEqual(total, 1)

        with self.assertRaisesRegex(ValueError, "limit"):
            self.store.get_category_entries("pipeline", limit=0)
        with self.assertRaisesRegex(ValueError, "offset"):
            self.store.get_category_entries("pipeline", offset=-1)

        self.assertIsNone(self.store.start_seeding("missing", "missing"))
        self.store.register(releases[3])
        self.assertIsNone(self.store.start_seeding(releases[3].id, releases[3].sd_hash))

        fresh = JobStore(self.root / "empty.sqlite3", clock=lambda: self.now)
        self.assertIsNone(fresh.next_seeding_delay())
        locked = JobStore(
            self.root / "nolock.sqlite3", clock=lambda: self.now, nolock=True
        )
        self.assertEqual(locked.get_setting("absent", default="fallback"), "fallback")
        locked.set_setting("github_token", "token-value")
        self.assertEqual(locked.get_setting("github_token"), "token-value")

    def test_migrated_rows_unknown_platform_and_json_fallbacks(self) -> None:
        release = self._release(11)
        torrent = TorrentArtifact(
            file_path=self.root / "gone.bin",
            torrent_path=self.root / "gone.torrent",
            piece_length=32,
            piece_count=2,
            info_hash="a" * 40,
            torrent_sha256="b" * 64,
            magnet_uri="magnet:?xt=urn:btih:" + "a" * 40,
            trackers=(),
        )
        self.store.record_migrated(
            release,
            file_path=torrent.file_path,
            sha384="c" * 96,
            sha256="d" * 64,
            torrent=torrent,
        )
        job = self.store.get(release.id, release.sd_hash)
        self.assertEqual(job.state, JobState.AWAITING_INDEX)
        self.assertEqual(job.payload_size, release.size)

        self.store._update(release)
        self.store._publication_update([])
        self.store._publication_update([(release.id, release.sd_hash)])
        self.store._seeding_update(release.id, release.sd_hash)

        with closing_connection(self.store) as connection:
            connection.execute(
                "UPDATE jobs SET platform=? WHERE release_id=?",
                ("custom", release.id),
            )
        breakdown = self.store.platform_breakdown()
        self.assertEqual(breakdown["custom"]["total"], 1)
        self.assertEqual(breakdown["custom"]["display_name"], "Custom")

        self.store.save_tracker_policy_cache(
            "https://index.example/policy", "etag", b'{"ok":true}'
        )
        with closing_connection(self.store) as connection:
            connection.execute(
                "UPDATE tracker_policy_cache SET document=? WHERE singleton=1",
                ('{"ok":true}',),
            )
        cached = self.store.load_tracker_policy_cache("https://index.example/policy")
        self.assertEqual(cached.document, b'{"ok":true}')

        self.assertEqual(
            _archive_fields_from_json("rid", "not-json"),
            ("rid", "Unknown channel", "rid"),
        )
        self.assertEqual(
            _archive_fields_from_json("rid", "[]"),
            ("rid", "Unknown channel", "rid"),
        )
        self.assertIsNone(_release_size_from_json("{"))
        self.assertIsNone(_release_size_from_json("[]"))
        self.assertIsNone(_release_size_from_json('{"origin":{"size":"nope"}}'))
        self.assertEqual(_platform_from_json("{"), "lbry")
        self.assertEqual(_platform_from_json("[]"), "lbry")
        self.assertEqual(_platform_from_json('{"origin":{"platform":"nope"}}'), "lbry")

        class Bare:
            raw = {"origin": "nope"}
            name = "bare-name"

        self.assertEqual(_release_slug(Bare()), "bare-name")

        with closing_connection(self.store) as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    release_id, sd_hash, release_json, release_name,
                    channel_handle, release_slug, platform, state, updated_at
                ) VALUES (?, ?, ?, '', '', '', '', 'pending', 1)
                """,
                ("legacy-id", "sd", "not-json"),
            )
            self.store._backfill_archive_fields(connection)
            self.store._backfill_platform_and_size(connection)
        legacy = self.store.get("legacy-id", "sd")
        self.assertEqual(legacy.platform, "lbry")
        self.assertEqual(legacy.release_id, "legacy-id")


class closing_connection:
    def __init__(self, store: JobStore):
        self.store = store
        self.connection = None

    def __enter__(self):
        self.connection = self.store._connect()
        return self.connection

    def __exit__(self, *exc: object) -> None:
        assert self.connection is not None
        self.connection.commit()
        self.connection.close()


class MigrationGapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()

    def _db(self, rows: list[tuple[str, str, str]]) -> None:
        lbry = self.data / "lbry" / "lbrynet"
        lbry.mkdir(parents=True)
        connection = sqlite3.connect(lbry / "lbrynet.sqlite")
        connection.execute(
            """
            CREATE TABLE file (
                stream_hash char(96), file_name text, download_directory text,
                status text, saved_file integer
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE stream (
                stream_hash char(96), sd_hash char(96), suggested_filename text
            )
            """
        )
        for index, (sd_hash, file_name, directory) in enumerate(rows):
            connection.execute(
                "INSERT INTO stream VALUES (?, ?, ?)",
                (f"s{index}", sd_hash, file_name),
            )
            connection.execute(
                "INSERT INTO file VALUES (?, ?, ?, 'stopped', 1)",
                (
                    f"s{index}",
                    file_name.encode().hex(),
                    directory.encode().hex(),
                ),
            )
        connection.commit()
        connection.close()

    def test_helpers_and_cli_cover_failure_branches(self) -> None:
        self.assertEqual(decode_hex_string(12), "12")
        self.assertEqual(decode_hex_string("ff"), "ff")
        self.assertEqual(decode_hex_string("00"), "00")
        with self.assertRaises(FileNotFoundError):
            load_bootstrap_index(self.root / "missing.zip")
        with self.assertRaises(FileNotFoundError):
            read_v1_lbrynet_db(self.root / "missing.sqlite")
        self.assertIsNone(read_v1_meta_json(self.root))
        (self.root / "meta.json").write_text("{")
        self.assertIsNone(read_v1_meta_json(self.root))

        payload = synthesize_v2_payload(
            release_id="a" * 40,
            name="Only LBRY",
            channel_handle="@chan:1",
            sd_hash="b" * 96,
            size=4,
            checksum="c" * 96,
        )
        self.assertTrue(
            any(
                link["url"].startswith("lbry://") for link in payload["origin"]["links"]
            )
        )

        self._db([])
        with patch(
            "sys.argv",
            [
                "migration",
                "--data-dir",
                str(self.data),
                "--dry-run",
                "--verbose",
                "--no-generate-missing",
                "--no-detect-faulty",
                "--nolock",
                "--no-staging-db",
                "--max-items",
                "1",
                "--verify-hashes",
            ],
        ):
            main()

    def test_runner_records_missing_faulty_and_direct_store_edges(self) -> None:
        sd_hash = "d" * 96
        self._db(
            [(sd_hash, "missing.zip", "/data/mirror/Author/Claim")]
            + [
                (f"{index:096x}", "gone.zip", f"/data/mirror/A/{index}")
                for index in range(100)
            ]
        )
        mirror = self.data / "mirror"
        author = mirror / "author"
        author.mkdir(parents=True)
        (author / "notes.txt").write_text("not a release")
        (author / "short").mkdir()
        empty = author / ("ab" * 20)
        empty.mkdir()
        meta_only = author / ("cd" * 20)
        meta_only.mkdir()
        (meta_only / "meta.json").write_text("{}")
        real = author / ("ef" * 20)
        real.mkdir()
        (real / "payload.zip").write_bytes(b"ok")
        blocked = mirror / "blocked"
        blocked.mkdir()
        blocked.chmod(0)
        mirror.chmod(0o755)
        try:
            config = MigrationConfig(
                data_dir=self.data,
                dry_run=True,
                detect_faulty_folders=True,
                max_items=2,
                logger=logging.getLogger("test-migration"),
            )
            stats = V1MigrationRunner(config).run()
        finally:
            blocked.chmod(0o755)
        self.assertGreaterEqual(stats.missing_files, 1)
        self.assertGreaterEqual(stats.faulty_stubs_ignored, 2)

        direct = MigrationConfig(
            data_dir=self.data,
            staging_db=False,
            sqlite_nolock=True,
            generate_missing_torrents=False,
            detect_faulty_folders=False,
            logger=logging.getLogger("test-migration-direct"),
        )
        with patch(
            "guncadmirror.migration.V1MigrationRunner._migrate_single_stream",
            side_effect=RuntimeError("explode"),
        ):
            failed = V1MigrationRunner(direct).run()
        self.assertGreaterEqual(failed.errors, 1)
        self.assertTrue(any("explode" in issue for issue in failed.issues))

        runner = V1MigrationRunner(
            MigrationConfig(
                data_dir=self.data,
                dry_run=True,
                detect_faulty_folders=False,
                logger=logging.getLogger("test-migration-single"),
            )
        )
        runner.stats.errors = 0
        runner._migrate_single_stream(
            {
                "sd_hash": sd_hash,
                "file_name": "missing.zip",
                "download_directory": "/data/mirror/Author/Claim",
            },
            {},
            None,
            None,
        )
        self.assertEqual(runner.stats.missing_files, 1)

        payload_dir = self.data / "mirror" / "Author" / "Claim"
        payload_dir.mkdir(parents=True)
        payload_file = payload_dir / "model.zip"
        payload_file.write_bytes(b"model-bytes")
        (payload_dir / "meta.json").write_text(
            json.dumps({"name": "No Id", "channel": {}, "torrent": {"btih": "a" * 40}})
        )
        runner._migrate_single_stream(
            {
                "sd_hash": sd_hash,
                "file_name": "model.zip",
                "download_directory": "/data/mirror/Author/Claim",
            },
            {},
            None,
            None,
        )
        self.assertTrue(any("No release ID" in issue for issue in runner.stats.issues))

        with patch("guncadmirror.migration.shutil.copy2", side_effect=OSError("copy")):
            copied = V1MigrationRunner(
                MigrationConfig(
                    data_dir=self.data,
                    staging_db=True,
                    detect_faulty_folders=False,
                    max_items=0,
                    logger=logging.getLogger("test-migration-copy"),
                )
            )
            (self.data / "mirror-state.sqlite3").write_bytes(b"sqlite-bytes")
            copied.local_state_db = self.root / "staged.sqlite3"
            # Constructor already ran; force the copy branch by rebuilding.
        staged = MigrationConfig(
            data_dir=self.data,
            staging_db=True,
            detect_faulty_folders=False,
            max_items=1,
            logger=logging.getLogger("test-migration-stage"),
        )
        (self.data / "mirror-state.sqlite3").write_bytes(b"")
        with patch("guncadmirror.migration.shutil.copy2", side_effect=OSError("copy")):
            V1MigrationRunner(staged)


class PipelineGapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = Settings(
            endpoint="https://index.example/api/v2/releases/",
            data_dir=self.root,
            min_free_space=0,
            retry_backoff=1,
            torrent_piece_length=16 * 1024,
            torrent_trackers=(),
        )
        self.store = JobStore(self.settings.state_path, clock=lambda: 10.0)
        self.publisher = OutboxPublisher(self.settings.outbox_dir)

    def _pipeline(self, releases: list[object], **kwargs: object) -> MirrorPipeline:
        class Index:
            def releases(self, **_kwargs: object):
                return iter(releases)

        return MirrorPipeline(
            self.settings,
            Index(),
            Mock(),
            self.store,
            self.publisher,
            disk_free=lambda _path: 10**12,
            **kwargs,
        )

    def test_cycle_routes_platforms_and_isolates_enumeration_errors(self) -> None:
        platforms = ("printables", "github", "http", "torrent")
        releases = []
        for index, platform in enumerate(platforms, start=1):
            raw = _platform_release(platform, f"{platform}-{index}")
            raw["name"] = f"{platform} model"
            raw["origin"]["size"] = 3
            raw["origin"]["checksum"] = None
            releases.append(type(make_release()).from_api(raw))

        pipeline = self._pipeline(releases)

        def acquire(prepared, stop=None):
            path = prepared.directory / "payload.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"xyz")
            return (
                pipeline._acquire_http.__wrapped__
                if False
                else __import__(
                    "guncadmirror.pipeline", fromlist=["_AcquiredJob"]
                )._AcquiredJob(
                    prepared,
                    path,
                    AcquisitionEvidence(AcquisitionTransport.HTTP),
                )
            )

        acquired = __import__(
            "guncadmirror.pipeline", fromlist=["_AcquiredJob"]
        )._AcquiredJob

        def fake_acquire(prepared, stop=None):
            path = prepared.directory / "payload.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"xyz")
            return acquired(
                prepared,
                path,
                AcquisitionEvidence(AcquisitionTransport.HTTP),
            )

        pipeline._acquire_printables = fake_acquire
        pipeline._acquire_github = fake_acquire
        pipeline._acquire_http = fake_acquire
        pipeline._acquire_torrent = fake_acquire
        result = pipeline.run_cycle()
        self.assertEqual(result.ready, 4)
        self.assertEqual(result.discovered, 4)

        class CancelIndex:
            def releases(self, **_kwargs: object):
                return _RaisingIter(AcquisitionCancelled())

        class BoomIndex:
            def releases(self, **_kwargs: object):
                return _RaisingIter(RuntimeError("index down"))

        class ClosingIndex:
            def __init__(self) -> None:
                self.closed = False

            def releases(self, **_kwargs: object):
                iterator = _RaisingIter(AcquisitionCancelled())
                iterator.close = self._close
                return iterator

            def _close(self) -> None:
                self.closed = True

        cancelled = MirrorPipeline(
            self.settings,
            CancelIndex(),
            Mock(),
            self.store,
            self.publisher,
            disk_free=lambda _path: 10**12,
        )
        self.assertEqual(cancelled.run_cycle().stopped, 1)
        closer = ClosingIndex()
        closing_pipeline = MirrorPipeline(
            self.settings,
            closer,
            Mock(),
            self.store,
            self.publisher,
            disk_free=lambda _path: 10**12,
        )
        self.assertEqual(closing_pipeline.run_cycle().stopped, 1)
        self.assertTrue(closer.closed)

        broken = MirrorPipeline(
            self.settings,
            BoomIndex(),
            Mock(),
            self.store,
            self.publisher,
            disk_free=lambda _path: 10**12,
        )
        with self.assertRaisesRegex(RuntimeError, "index down"):
            broken.run_cycle()

        prepare_boom = self._pipeline([make_release()])
        prepare_boom._prepare = Mock(side_effect=RuntimeError("prepare failed"))
        with self.assertRaisesRegex(RuntimeError, "prepare failed"):
            prepare_boom.run_cycle()

    def test_prepare_stop_register_and_artifact_guards(self) -> None:
        release = make_release()
        pipeline = self._pipeline([release])
        stop = Event()
        stop.set()
        self.assertEqual(pipeline.process(release, stop=stop), "stopped")

        pipeline._disk_budget.acquire(
            2 * (release.size or 0),
            data_dir=self.root,
            reserve=0,
            disk_free=lambda _path: 2 * (release.size or 0),
        )
        contended = MirrorPipeline(
            self.settings,
            Mock(),
            Mock(),
            self.store,
            self.publisher,
            disk_free=lambda _path: 2 * (release.size or 0),
        )
        contended._disk_budget = pipeline._disk_budget
        self.assertEqual(contended.process(release), "skipped")

        failing = self._pipeline([release])
        with patch.object(self.store, "register", side_effect=RuntimeError("db")):
            with self.assertRaisesRegex(RuntimeError, "db"):
                failing.process(release)

        start_fail = self._pipeline(
            [make_release(release_id="1" * 40, sd_hash="2" * 96)]
        )
        with patch.object(
            self.store, "start_attempt", side_effect=RuntimeError("start")
        ):
            self.assertEqual(
                start_fail.process(make_release(release_id="1" * 40, sd_hash="2" * 96)),
                "failed",
            )

        reservation = pipeline._disk_budget.acquire(
            1,
            data_dir=self.root,
            reserve=0,
            disk_free=lambda _path: 100,
        )[0]
        assert reservation is not None
        reservation.release()
        reservation.release()

        self.assertTrue(_valid_acquisition_document(None))
        self.assertFalse(_valid_acquisition_document("nope"))

        job = self.store.register(release)
        self.assertFalse(pipeline._ready_artifacts_exist(job, release))
        awaiting = self.store.get(release.id, release.sd_hash)
        self.assertIsNotNone(awaiting)
        manifest = self.root / "not-object.json"
        manifest.write_text("[]")
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "stat") as stat,
        ):
            stat.return_value.st_size = release.size or 1
            self.assertFalse(pipeline._ready_artifacts_exist(job, release))


class _RaisingIter:
    def __init__(self, error: Exception):
        self.error = error

    def __iter__(self):
        return self

    def __next__(self):
        raise self.error


class AcquirerGapTests(unittest.TestCase):
    def test_printables_protocol_and_retry_edges(self) -> None:
        sleeps: list[float] = []
        http_error = requests.HTTPError("429 Client Error")
        http_error.response = FakeResponse(
            status_code=429, headers={"Retry-After": "soon"}
        )
        session = QueueSession(
            FakeResponse(["nope"]),
            FakeResponse({"errors": ["bad"]}),
        )
        acquirer = PrintablesAcquirer(
            session=session,
            attempts=1,
            backoff=0,
            pacing=0,
            sleep=sleeps.append,
        )
        acquirer.close()
        self.assertTrue(session.closed)
        release = type(make_release()).from_api(
            _platform_release(
                "printables",
                "printables-77",
                external_id="not-digits",
                url="https://www.printables.com/model/77-widget",
            )
        )
        self.assertEqual(acquirer._extract_model_id(release), "77")
        bare = type(make_release()).from_api(
            _platform_release("printables", "printables-88", external_id="")
        )
        self.assertEqual(acquirer._extract_model_id(bare), "88")
        broken = type(make_release()).from_api(
            _platform_release("printables", "printables-nope", external_id="nope")
        )
        with self.assertRaises(PrintablesProtocolError):
            acquirer._extract_model_id(broken)

        with self.assertRaises(PrintablesProtocolError):
            acquirer._graphql_post("query {}", {}, "Op")
        with self.assertRaises(PrintablesProtocolError):
            acquirer._graphql_post("query {}", {}, "Op")
        acquirer.session = QueueSession(requests.ConnectionError("reset"))
        with self.assertRaises(PrintablesUnavailable):
            acquirer._graphql_post("query {}", {}, "Op")
        acquirer.session = QueueSession(FakeResponse({"data": {"model": None}}))
        self.assertEqual(acquirer._fetch_model_files("1"), [])
        paced = PrintablesAcquirer(
            session=QueueSession(http_error, FakeResponse({"data": {"ok": True}})),
            attempts=2,
            backoff=1,
            pacing=0,
            sleep=sleeps.append,
        )
        self.assertEqual(paced._graphql_post("q", {}, "Op"), {"ok": True})

        empty_link = QueueSession(FakeResponse({"getDownloadLink": {"ok": False}}))
        acquirer.session = empty_link
        acquirer.attempts = 1
        with self.assertRaises(PrintablesUnavailable):
            acquirer._get_download_link("f", "1", "stl")

        retry = PrintablesAcquirer(
            session=QueueSession(
                requests.ConnectionError("down"),
                FakeResponse({"data": {"model": None}}),
            ),
            attempts=2,
            backoff=1,
            pacing=0,
            sleep=sleeps.append,
        )
        self.assertEqual(retry._graphql_post("q", {}, "ModelFiles")["model"], None)
        self.assertIn(1, sleeps)

    def test_github_parse_and_request_failure(self) -> None:
        session = QueueSession(requests.ConnectionError("down"))
        client = GitHubAcquirer(session=session, token=None)
        client.token = "configured-token"
        self.assertEqual(client.token, "configured-token")
        client.close()
        self.assertTrue(session.closed)
        release = type(make_release()).from_api(
            _platform_release(
                "github",
                "github-org-repo",
                external_id="octo/repo/v1",
            )
        )
        self.assertEqual(client._parse_target(release), ("octo", "repo", "v1"))
        nameless = type(make_release()).from_api(
            _platform_release("github", "github-only", external_id="only")
        )
        with self.assertRaises(GitHubProtocolError):
            client._parse_target(nameless)
        url, name = client._resolve_download_target("octo", "repo", "v1")
        self.assertIn("/zipball/v1", url)
        self.assertTrue(name.endswith(".zip"))

    def test_http_download_and_acquirer_edges(self) -> None:
        destination = Path(tempfile.mkdtemp()) / "file.bin"

        class Response:
            def __init__(self, chunks, headers=None, error=None):
                self.headers = headers or {}
                self.error = error
                self._chunks = chunks
                self.url = "https://files.example/final.bin"

            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

            def raise_for_status(self) -> None:
                if self.error:
                    raise self.error

            def iter_content(self, chunk_size: int = 1):
                yield from self._chunks

        class Session:
            def __init__(self, responses):
                self.responses = list(responses)
                self.closed = False

            def get(self, url, **kwargs):
                item = self.responses.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

            def head(self, url, **kwargs):
                return self.responses.pop(0)

            def close(self) -> None:
                self.closed = True

        rate = requests.HTTPError("429")
        rate.response = Mock(status_code=429, headers={"Retry-After": "nope"})
        session = Session(
            [
                Response([b"", b"abcd"], headers={"Content-Length": "4"}),
            ]
        )
        size = download_url_to_file(
            "https://files.example/a",
            destination,
            headers={"Accept": "application/octet-stream"},
            session=session,
            attempts=1,
            progress=lambda done, total: None,
        )
        self.assertEqual(size, 4)
        self.assertEqual(destination.read_bytes(), b"abcd")

        sleeps: list[float] = []
        failing = Session([rate, requests.ConnectionError("later")])
        with self.assertRaises(DownloadError):
            download_url_to_file(
                "https://files.example/b",
                destination,
                session=failing,
                attempts=1,
                backoff=0,
                sleep=sleeps.append,
            )

        def explode(done: int, total: int | None) -> None:
            raise RuntimeError("progress")

        exploding = Session(
            [
                Response([b"zz"], headers={"Content-Length": "2"}),
                requests.ConnectionError("x"),
            ]
        )
        with self.assertRaises(RuntimeError):
            download_url_to_file(
                "https://files.example/c",
                destination,
                session=exploding,
                attempts=1,
                backoff=1,
                progress=explode,
                sleep=sleeps.append,
            )

        release = type(make_release()).from_api(
            _platform_release(
                "http",
                "http-1",
                links=[
                    "skip",
                    {"url": ""},
                    {"url": "lbry://claim"},
                    {"url": "https://files.example/download", "download": True},
                ],
            )
        )
        head = Mock(
            status_code=200,
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": "attachment; filename*=UTF-8''model.zip",
            },
            url="https://cdn.example/noext",
        )
        http = HttpAcquirer(session=Session([head]), sleep=lambda _delay: None)
        http.close()
        self.assertEqual(http._resolve_url(release), "https://files.example/download")
        self.assertIsNone(parse_content_disposition_filename("attachment"))
        named = type(make_release()).from_api(
            _platform_release("http", "http-2", url="https://files.example/get")
        )
        named_session = Session([head])
        http.session = named_session
        self.assertTrue(
            http._determine_filename("https://files.example/get", named).endswith(
                ".zip"
            )
        )
        zip_named = type(make_release()).from_api(
            _platform_release("http", "http-3", url="https://files.example/already")
        )
        zip_named_raw = zip_named
        object.__setattr__ if False else None
        fallback = HttpAcquirer(
            session=Session([Mock(status_code=500)]), sleep=lambda _d: None
        )
        from dataclasses import replace

        zip_release = replace(zip_named_raw, name="Already.zip")
        self.assertEqual(
            fallback._determine_filename("https://files.example/no-name", zip_release),
            "Already.zip",
        )

    def test_torrent_resolution_and_packaging(self) -> None:
        self.assertIsNone(extract_info_hash_from_magnet("magnet:?xt=urn:btih:AAAA"))
        with patch(
            "guncadmirror.torrent_acquirer.base64.b32decode",
            side_effect=ValueError("bad"),
        ):
            self.assertIsNone(
                extract_info_hash_from_magnet("magnet:?xt=urn:btih:" + "A" * 32)
            )
        settings = Settings(
            endpoint="https://index.example/api/v2/releases/",
            data_dir=Path(tempfile.mkdtemp()),
        )
        session = QueueSession()
        client = Mock()
        acquirer = TorrentAcquirer(settings, client=client, session=session)
        acquirer.close()
        self.assertTrue(session.closed)
        release = type(make_release()).from_api(
            _platform_release(
                "torrent",
                "torrent-1",
                links=[
                    "nope",
                    {"url": "  "},
                    {"url": "/torrents/file.torrent", "name": "torrent"},
                ],
            )
        )
        probed = Mock(status_code=200, content=b"not-a-torrent")
        acquirer.session = Mock(get=Mock(return_value=probed), close=Mock())
        with self.assertRaises(TorrentAcquisitionError):
            acquirer._resolve_source(release)
        client.add_url.side_effect = QBitError("add", "rejected")
        magnet = type(make_release()).from_api(
            _platform_release(
                "torrent",
                "torrent-2",
                links=[{"url": "magnet:?xt=urn:btih:" + "ab" * 20}],
            )
        )
        with self.assertRaises(TorrentAcquisitionUnavailable):
            acquirer.acquire(magnet, settings.data_dir / "out")
        output = settings.data_dir / "multi"
        output.mkdir()
        (output / "a.txt").write_bytes(b"a")
        (output / "b.txt").write_bytes(b"b")
        packaged = acquirer._locate_payload(output, magnet)
        self.assertTrue(packaged.name.endswith(".zip"))
        empty = settings.data_dir / "empty"
        empty.mkdir()
        with self.assertRaises(TorrentAcquisitionError):
            acquirer._locate_payload(empty, magnet)


class PublicationAndServiceGapTests(unittest.TestCase):
    def test_publication_preparation_and_delay_edges(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        settings = Settings(
            endpoint="https://index.example/api/v2/releases/",
            data_dir=root,
            publish_enabled=False,
        )
        store = JobStore(settings.state_path, clock=lambda: 5.0)
        scheduler = PublicationScheduler(settings, store, Mock())
        self.assertIsNone(scheduler.next_delay())
        settings_enabled = Settings(
            endpoint=settings.endpoint,
            data_dir=root,
            publish_enabled=True,
            publish_url="https://index.example/api/v2/publications/",
            publish_token="token",
        )
        enabled = PublicationScheduler(settings_enabled, store, Mock())
        self.assertIsNone(enabled.next_delay())

        release = make_release()
        store.register(release)
        store.start_attempt(release)
        payload = root / "payload.bin"
        payload.write_bytes(b"payload")
        torrent_path = root / "out.torrent"
        torrent_path.write_bytes(b"torrent")
        store.mark_verified(
            release,
            file_path=payload,
            sha384="a" * 96,
            sha256="b" * 64,
        )
        store.mark_awaiting_index(
            release,
            TorrentArtifact(
                file_path=payload,
                torrent_path=torrent_path,
                piece_length=16,
                piece_count=1,
                info_hash="c" * 40,
                torrent_sha256="d" * 64,
                magnet_uri="magnet:?xt=urn:btih:" + "c" * 40,
                trackers=(),
            ),
        )
        from dataclasses import replace

        from guncadmirror.state import PublicationCandidate

        job = store.get(release.id, release.sd_hash)
        assert release.sha384 is not None
        staged_torrent = (
            settings_enabled.outbox_dir / release.id / release.sd_hash / "file.torrent"
        )
        staged_torrent.parent.mkdir(parents=True, exist_ok=True)
        staged_torrent.write_bytes(b"torrent")
        job = replace(
            job,
            sha384=release.sha384,
            sha256="ab" * 32,
            info_hash="c" * 40,
            file_path=payload,
            torrent_path=staged_torrent,
        )
        with patch.object(
            store,
            "publication_candidates",
            return_value=[PublicationCandidate(release, replace(job, sha384=None))],
        ):
            self.assertIsNone(enabled.next_delay())
        with self.assertRaisesRegex(PublicationPreparationError, "incomplete"):
            prepare_submission(
                settings_enabled,
                PublicationCandidate(release, replace(job, sha256="nope")),
            )
        with self.assertRaisesRegex(
            PublicationPreparationError, "contradicts the Index"
        ):
            prepare_submission(
                settings_enabled,
                PublicationCandidate(release, replace(job, sha384="e" * 96)),
            )
        with self.assertRaisesRegex(PublicationPreparationError, "no payload"):
            prepare_submission(
                settings_enabled,
                PublicationCandidate(release, replace(job, file_path=None)),
            )
        with self.assertRaises(PublicationPreparationError):
            prepare_submission(
                settings_enabled,
                PublicationCandidate(
                    release, replace(job, file_path=root / "outside" / "payload.bin")
                ),
            )
        with self.assertRaisesRegex(PublicationPreparationError, "missing or empty"):
            prepare_submission(
                settings_enabled,
                PublicationCandidate(
                    release,
                    replace(job, file_path=root / "missing-payload.bin"),
                ),
            )
        with self.assertRaisesRegex(PublicationPreparationError, "torrent"):
            prepare_submission(
                settings_enabled,
                PublicationCandidate(
                    release,
                    replace(
                        job,
                        torrent_path=settings_enabled.outbox_dir / "missing.torrent",
                    ),
                ),
            )
        skipped = enabled._process_sha_group(
            [
                PublicationCandidate(
                    release,
                    replace(job, publication_next_attempt_at=0, sha384="a" * 96),
                )
            ],
            Event(),
            None,
        )
        self.assertEqual(skipped.attempted, 0)

        manifest = (
            settings_enabled.outbox_dir / release.id / release.sd_hash / "manifest.json"
        )
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{")
        store._update(release, torrent_path=str(torrent_path))
        # torrent bytes are not valid; parse error happens before manifest read
        # when torrent exists. Write a contradictory identity after a valid torrent
        # is too heavy; exercise manifest helpers through a patched parse.
        with (
            patch(
                "guncadmirror.publication.parse_torrent",
                return_value=Mock(
                    file_name=payload.name,
                    file_length=payload.stat().st_size,
                    info_hash="c" * 40,
                    magnet_uri="magnet:?xt=urn:btih:" + "c" * 40,
                    trackers=(),
                    piece_length=16,
                    piece_count=1,
                    torrent_sha256="d" * 64,
                ),
            ),
            patch(
                "guncadmirror.publication.strip_torrent_trackers",
                return_value=b"raw",
            ),
        ):
            candidate = PublicationCandidate(release, job)
            with self.assertRaisesRegex(PublicationPreparationError, "manifest"):
                prepare_submission(settings_enabled, candidate)
            manifest.write_text(json.dumps({"schema": "nope"}))
            with self.assertRaisesRegex(PublicationPreparationError, "schema"):
                prepare_submission(settings_enabled, candidate)
            manifest.write_text(
                json.dumps(
                    {
                        "schema": REQUEST_SCHEMA,
                        "release": "bad",
                    }
                )
            )
            with self.assertRaisesRegex(PublicationPreparationError, "sections"):
                prepare_submission(settings_enabled, candidate)
            manifest.write_text(
                json.dumps(
                    {
                        "schema": REQUEST_SCHEMA,
                        "release": {"id": release.id},
                        "lbry": {"sd_hash": release.sd_hash},
                        "artifact": {"sha384": release.sha384, "sha256": "ab" * 32},
                        "torrent": {"btih": "c" * 40},
                        "acquisition": "bad",
                    }
                )
            )
            with self.assertRaisesRegex(PublicationPreparationError, "evidence"):
                prepare_submission(settings_enabled, candidate)
            document = json.loads(manifest.read_text())
            document["acquisition"] = {"transport": "carrier-pigeon"}
            manifest.write_text(json.dumps(document))
            with self.assertRaisesRegex(PublicationPreparationError, "transport"):
                prepare_submission(settings_enabled, candidate)

            document["acquisition"] = {"transport": "lbry"}
            manifest.write_text(json.dumps(document))
            mismatched = PublicationCandidate(release, replace(job, info_hash="f" * 40))
            with self.assertRaisesRegex(PublicationPreparationError, "contradicts"):
                prepare_submission(settings_enabled, mismatched)

    def test_settings_qbit_runtime_index_and_web_edges(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "invalid value"):
            Settings.from_env(
                {
                    "MIRROR_QBITTORRENT_ENABLED": "1",
                    "MIRROR_QBITTORRENT_USERNAME": "mirror\nuser",
                    "MIRROR_QBITTORRENT_PASSWORD": "secret",
                }
            )

        with self.assertRaises(PublicationPaused):
            _mapping({}, "receipt")
        with self.assertRaises(PublicationPaused):
            _absolute_http_url("not a url", "canonical torrent URL")
        with self.assertRaises(PublicationPaused):
            _canonical_magnet(None, "a" * 40)
        self.assertIsInstance(
            _retry_after("Wed, 01 Jan 2020 00:00:00", lambda: 0), float
        )
        self.assertEqual(_retry_after_seconds("   ", 0), None)
        self.assertIsNone(_retry_after_seconds("not-a-date", 0))
        self.assertGreater(
            _retry_after_seconds("Wed, 01 Jan 2099 00:00:00 GMT", 0) or 0, 0
        )

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = Settings.from_env(
            {
                "MIRROR_DATA_DIR": temporary.name,
                "MIRROR_ENDPOINT": "https://index.example/api/v2/releases/",
            }
        )
        store = JobStore(settings.state_path)
        release = make_release()
        store.register(release)
        store._update(release, publication_state=PublicationState.PUBLISHING)
        store._update(release, seeding_state=SeedingState.INJECTING)
        with (
            patch("guncadmirror.runtime.LbryClient"),
            patch("guncadmirror.runtime.IndexClient"),
            patch("guncadmirror.runtime.OdyseeAcquirer"),
            patch("guncadmirror.runtime.MirrorPipeline"),
        ):
            runtime = build_runtime(settings)
        self.assertIsNotNone(runtime)
        runtime.lbry_ready = True
        runtime._ensure_lbry(None)
        self.assertIsInstance(runtime, Runtime)

        session = QueueSession(
            FakeResponse({"results": [], "next": None}),
        )
        client = IndexClient(
            "https://index.example/api/v2/releases/",
            session=session,
            max_pages=3,
            max_releases=None,
            attempts=1,
        )
        self.assertEqual(list(client.releases()), [])

        self.assertFalse(_is_ready("nope"))
        lbry_client = Mock()
        acquirer = LbryAcquirer(
            lbry_client,
            data_root=Path(temporary.name),
            download_timeout=1,
            poll_interval=0.01,
        )
        lbry_client.call.side_effect = LbryError("stop failed")
        acquirer._stop_timed_out_stream(make_release())
        lbry_client.call.side_effect = None
        lbry_client.file_for_sd_hash.return_value = None
        lbry_client.call.return_value = ["not-a-mapping"]
        with self.assertRaises(LbryProtocolError):
            acquirer.acquire(make_release(), Path(temporary.name) / "lbry-out")

        self.assertEqual(humanize_bytes(object()), "0.0 B")
        self.assertEqual(humanize_seconds(object()), "0.0 seconds")
        collector = StatsCollector(settings, store)
        app = create_app(collector)
        http = app.test_client()
        missing = http.get(f"/archive/{'a' * 40}/{'b' * 96}/payload")
        self.assertEqual(missing.status_code, 404)
        overlong = http.get("/api/entries?q=" + "x" * 201)
        self.assertEqual(overlong.status_code, 400)
        page = http.get("/archive?page=9")
        self.assertEqual(page.status_code, 200)
        retry = http.post("/api/jobs/retry", data={"platform": "lbry"})
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.get_json()["retried"], 0)

        errors: list[str] = []
        _validate_seeding(
            sqlite_row(
                seeding_state="retrying",
                seeding_attempts=0,
                seeding_next_attempt_at=0,
                seeding_error_code="",
                seeding_error="",
            ),
            errors,
        )
        self.assertTrue(errors)
        self.assertFalse(_is_safe_http_url(None))
        path_errors: list[str] = []
        self.assertIsNone(_safe_path(Path(temporary.name), "", "payload", path_errors))
        magnet_errors: list[str] = []
        _validate_magnet(None, None, magnet_errors)
        self.assertTrue(magnet_errors)
        acq_errors: list[str] = []
        self.assertEqual(
            _acquisition_fields(
                {"transport": "odysee-cdn", "source_url": None, "lbry_failure": "x"},
                make_release(),
                acq_errors,
            )[0],
            "odysee-cdn",
        )
        self.assertTrue(acq_errors)

        seed_settings = Settings(
            endpoint=settings.endpoint,
            data_dir=Path(temporary.name),
            qbittorrent_enabled=False,
        )
        seeding = SeedingScheduler(seed_settings, store, Mock())
        self.assertIsNone(seeding.next_delay())
        candidate = store.seeding_candidates()
        if candidate:
            job = candidate[0].job
            object.__setattr__(job, "file_path", None) if False else None
            from dataclasses import replace

            broken_job = replace(candidate[0].job, file_path=None)
            broken = replace(candidate[0], job=broken_job)
            with self.assertRaises(QBitError):
                prepare_seed(seed_settings, broken)


def sqlite_row(**values: object):
    connection = sqlite3.connect(":memory:")
    columns = ", ".join(values)
    connection.execute(f"CREATE TABLE t ({columns})")
    connection.execute(
        f"INSERT INTO t VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )
    connection.row_factory = sqlite3.Row
    return connection.execute("SELECT * FROM t").fetchone()


if __name__ == "__main__":
    unittest.main()
