from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from guncadmirror.migration import (
    MigrationConfig,
    V1MigrationRunner,
    compute_hashes,
    decode_hex_string,
    load_bootstrap_index,
    read_v1_lbrynet_db,
    relocate_qbit_seeds,
    synthesize_v2_payload,
)
from guncadmirror.models import JobState, Release, SeedingState, TorrentArtifact
from guncadmirror.settings import Settings
from guncadmirror.state import JobStore
from guncadmirror.torrent import create_torrent


class TestMigration(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_decode_hex_string(self) -> None:
        raw_hex = "/data/mirror".encode("utf-8").hex()
        self.assertEqual(decode_hex_string(raw_hex), "/data/mirror")
        self.assertEqual(decode_hex_string("/data/mirror"), "/data/mirror")
        self.assertEqual(decode_hex_string(f"'{raw_hex}'"), "/data/mirror")

    def test_synthesize_v2_payload_validates(self) -> None:
        payload = synthesize_v2_payload(
            release_id="0" * 40,
            name="Test Release",
            channel_handle="@testchannel:1",
            sd_hash="1" * 96,
            size=12345,
            checksum="2" * 96,
            url="https://odysee.com/@testchannel:1/test:0",
            url_lbry="lbry://@testchannel:1/test:0",
        )
        release = Release.from_api(payload)
        self.assertEqual(release.id, "0" * 40)
        self.assertEqual(release.name, "Test Release")
        self.assertEqual(release.channel_handle, "@testchannel:1")
        self.assertEqual(release.sd_hash, "1" * 96)
        self.assertEqual(release.size, 12345)
        self.assertEqual(release.sha384, "2" * 96)

    def test_load_bootstrap_index(self) -> None:
        zip_path = self.root / "bootstrap.zip"
        manifest_data = {
            "schema": "guncad-index-torrent-bootstrap-v1",
            "artifacts": [
                {
                    "btih": "a" * 40,
                    "sha384": "b" * 96,
                    "size": 100,
                    "torrent": "torrents/art1.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:" + "a" * 40,
                    "releases": [
                        {
                            "id": "c" * 40,
                            "name": "Release 1",
                            "channel": "@chan:1",
                            "sd_hash": "d" * 96,
                        }
                    ],
                }
            ],
        }
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest_data))

        index = load_bootstrap_index(zip_path)
        self.assertIn("d" * 96, index)
        art, rel = index["d" * 96]
        self.assertEqual(art["btih"], "a" * 40)
        self.assertEqual(rel["name"], "Release 1")

    def test_read_v1_lbrynet_db(self) -> None:
        db_path = self.data_dir / "lbrynet.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE file (
                stream_hash char(96),
                file_name text,
                download_directory text,
                status text,
                saved_file integer
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE stream (
                stream_hash char(96),
                sd_hash char(96),
                suggested_filename text
            )
            """
        )
        fn_hex = "test_file.zip".encode("utf-8").hex()
        dd_hex = "/data/mirror/test_author/test_rel".encode("utf-8").hex()
        conn.execute("INSERT INTO stream VALUES ('s1', 'sd1', 'test_file.zip')")
        conn.execute(
            f"INSERT INTO file VALUES ('s1', '{fn_hex}', '{dd_hex}', 'stopped', 1)"
        )
        conn.commit()
        conn.close()

        records = read_v1_lbrynet_db(db_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["file_name"], "test_file.zip")
        self.assertEqual(
            records[0]["download_directory"], "/data/mirror/test_author/test_rel"
        )
        self.assertEqual(records[0]["sd_hash"], "sd1")

    def test_migration_runner_end_to_end(self) -> None:
        lbry_dir = self.data_dir / "lbry" / "lbrynet"
        lbry_dir.mkdir(parents=True, exist_ok=True)
        db_path = lbry_dir / "lbrynet.sqlite"

        # Create dummy payload 1 (matched in bootstrap)
        release_dir1 = self.data_dir / "mirror" / "Author#a" / "Release#b"
        release_dir1.mkdir(parents=True, exist_ok=True)
        payload_file1 = release_dir1 / "test1.zip"
        dummy_content1 = b"GunCAD 3D Printable Content For Testing Migration 1"
        payload_file1.write_bytes(dummy_content1)
        hashes1 = compute_hashes(payload_file1)

        release_id1 = "1" * 40
        sd_hash1 = "2" * 96

        meta_json_path1 = release_dir1 / "meta.json"
        meta_json_path1.write_text(
            json.dumps(
                {
                    "id": release_id1,
                    "name": "Release B",
                    "channel": {"handle": "@Author:a"},
                    "sd_hash": sd_hash1,
                    "sha384sum": hashes1.sha384,
                    "size": hashes1.size,
                    "url": "https://odysee.com/@Author:a/Release-B:0",
                    "url_lbry": "lbry://@Author:a/Release-B:0",
                }
            )
        )

        # Create dummy payload 2 (DELISTED / RARE: absent from bootstrap zip)
        release_dir2 = self.data_dir / "mirror" / "Author#a" / "Delisted#c"
        release_dir2.mkdir(parents=True, exist_ok=True)
        payload_file2 = release_dir2 / "rare_model.step"
        dummy_content2 = b"Rare and delisted 3D model that was removed from index"
        payload_file2.write_bytes(dummy_content2)
        hashes2 = compute_hashes(payload_file2)

        release_id2 = "3" * 40
        sd_hash2 = "4" * 96

        meta_json_path2 = release_dir2 / "meta.json"
        meta_json_path2.write_text(
            json.dumps(
                {
                    "id": release_id2,
                    "name": "Delisted Rare Part",
                    "channel": {"handle": "@Author:a"},
                    "sd_hash": sd_hash2,
                    "sha384sum": hashes2.sha384,
                    "size": hashes2.size,
                    "url": "https://odysee.com/@Author:a/Delisted-Part:0",
                    "url_lbry": "lbry://@Author:a/Delisted-Part:0",
                }
            )
        )

        # Create a faulty empty stub directory named after release_id1
        faulty_dir = self.data_dir / "mirror" / "Author#a" / release_id1
        faulty_dir.mkdir(parents=True, exist_ok=True)
        (faulty_dir / "meta.json").write_text('{"id": "faulty"}')

        # Create mock lbrynet.sqlite
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE file (stream_hash text, file_name text, download_directory text, status text, saved_file int)"
        )
        conn.execute(
            "CREATE TABLE stream (stream_hash text, sd_hash text, suggested_filename text)"
        )
        fn_hex1 = "test1.zip".encode().hex()
        dd_hex1 = "/data/mirror/Author#a/Release#b".encode().hex()
        conn.execute(
            "INSERT INTO stream VALUES ('stream1', ?, 'test1.zip')", (sd_hash1,)
        )
        conn.execute(
            f"INSERT INTO file VALUES ('stream1', '{fn_hex1}', '{dd_hex1}', 'stopped', 1)"
        )

        fn_hex2 = "rare_model.step".encode().hex()
        dd_hex2 = "/data/mirror/Author#a/Delisted#c".encode().hex()
        conn.execute(
            "INSERT INTO stream VALUES ('stream2', ?, 'rare_model.step')", (sd_hash2,)
        )
        conn.execute(
            f"INSERT INTO file VALUES ('stream2', '{fn_hex2}', '{dd_hex2}', 'stopped', 1)"
        )

        conn.commit()
        conn.close()

        # Create dummy canonical torrent for release 1
        dummy_torrent_path = self.root / "canonical.torrent"
        torrent_artifact1 = create_torrent(
            payload_file1,
            dummy_torrent_path,
            piece_length=1048576,
        )
        canonical_torrent_bytes1 = dummy_torrent_path.read_bytes()

        # Create bootstrap ZIP containing only release 1
        zip_path = self.root / "bootstrap.zip"
        bootstrap_manifest = {
            "schema": "guncad-index-torrent-bootstrap-v1",
            "artifacts": [
                {
                    "btih": torrent_artifact1.info_hash,
                    "sha384": hashes1.sha384,
                    "size": hashes1.size,
                    "torrent": f"torrents/{torrent_artifact1.info_hash}.torrent",
                    "magnet_uri": torrent_artifact1.magnet_uri,
                    "releases": [
                        {
                            "id": release_id1,
                            "name": "Release B",
                            "channel": "@Author:a",
                            "sd_hash": sd_hash1,
                        }
                    ],
                }
            ],
        }
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps(bootstrap_manifest))
            zf.writestr(
                f"torrents/{torrent_artifact1.info_hash}.torrent",
                canonical_torrent_bytes1,
            )

        # 2. Run Migration Runner in DRY RUN mode
        config = MigrationConfig(
            data_dir=self.data_dir,
            bootstrap_zip=zip_path,
            target_prefix=Path("/data"),
            dry_run=True,
            verify_hashes=True,
            detect_faulty_folders=True,
        )
        runner = V1MigrationRunner(config)
        stats = runner.run()

        self.assertEqual(stats.total_local_streams, 2)
        self.assertEqual(stats.matched_bootstrap, 1)
        self.assertEqual(stats.delisted_or_unindexed, 1)
        self.assertEqual(stats.torrents_extracted, 1)
        self.assertEqual(stats.torrents_generated, 1)
        self.assertEqual(stats.faulty_stubs_ignored, 1)
        self.assertEqual(stats.missing_files, 0)
        self.assertEqual(stats.errors, 0)

        # In dry run, outbox and sqlite should NOT be written
        state_db = self.data_dir / "mirror-state.sqlite3"
        store = JobStore(state_db)
        self.assertEqual(store.counts(), {})

        # 3. Run Migration Runner in LIVE mode
        config.dry_run = False
        runner = V1MigrationRunner(config)
        live_stats = runner.run()

        self.assertEqual(live_stats.matched_bootstrap, 1)
        self.assertEqual(live_stats.delisted_or_unindexed, 1)
        self.assertEqual(live_stats.torrents_extracted, 1)
        self.assertEqual(live_stats.torrents_generated, 1)
        self.assertEqual(live_stats.faulty_stubs_ignored, 1)
        self.assertEqual(live_stats.errors, 0)

        # Check outbox files for Release 1
        outbox_torrent1 = (
            self.data_dir
            / "outbox"
            / release_id1
            / sd_hash1
            / f"{hashes1.sha384}.torrent"
        )
        outbox_manifest1 = (
            self.data_dir / "outbox" / release_id1 / sd_hash1 / "manifest.json"
        )
        self.assertTrue(outbox_torrent1.is_file())
        self.assertTrue(outbox_manifest1.is_file())

        # Check outbox files for Delisted Release 2
        outbox_torrent2 = (
            self.data_dir
            / "outbox"
            / release_id2
            / sd_hash2
            / f"{hashes2.sha384}.torrent"
        )
        outbox_manifest2 = (
            self.data_dir / "outbox" / release_id2 / sd_hash2 / "manifest.json"
        )
        self.assertTrue(outbox_torrent2.is_file())
        self.assertTrue(outbox_manifest2.is_file())

        # Verify JobStore has BOTH jobs in AWAITING_INDEX with canonical releases paths
        canonical_p1 = (
            self.data_dir
            / "releases"
            / "@Author#a"
            / f"Release-B-{sd_hash1[:12]}"
            / "test1.zip"
        )
        canonical_p2 = (
            self.data_dir
            / "releases"
            / "@Author#a"
            / f"Delisted-Rare-Part-{sd_hash2[:12]}"
            / "rare_model.step"
        )
        self.assertTrue(canonical_p1.is_file())
        self.assertTrue((canonical_p1.parent / "release.json").is_file())
        self.assertTrue(canonical_p2.is_file())
        self.assertTrue((canonical_p2.parent / "release.json").is_file())

        job1 = store.get(release_id1, sd_hash1)
        self.assertEqual(job1.state, JobState.AWAITING_INDEX)
        self.assertEqual(job1.seeding_state, SeedingState.PENDING)
        self.assertEqual(
            job1.file_path,
            Path(f"/data/releases/@Author#a/Release-B-{sd_hash1[:12]}/test1.zip"),
        )

        job2 = store.get(release_id2, sd_hash2)
        self.assertEqual(job2.state, JobState.AWAITING_INDEX)
        self.assertEqual(job2.seeding_state, SeedingState.PENDING)
        self.assertEqual(
            job2.file_path,
            Path(
                f"/data/releases/@Author#a/Delisted-Rare-Part-{sd_hash2[:12]}/rare_model.step"
            ),
        )
        self.assertEqual(job2.sha384, hashes2.sha384)
        self.assertEqual(live_stats.payloads_relocated, 2)

        # Verify idempotency on second run
        idempotent_stats = runner.run()
        self.assertEqual(idempotent_stats.skipped_existing, 2)

    @patch("guncadmirror.migration.QBitClient")
    def test_relocate_qbit_seeds(self, mock_client_cls: Mock) -> None:
        mock_client = mock_client_cls.return_value
        store = JobStore(self.data_dir / "mirror-state.sqlite3")
        release = Release.from_api(
            synthesize_v2_payload(
                release_id="0" * 40,
                name="Relocatable Release",
                channel_handle="@author:1",
                sd_hash="1" * 96,
                size=10,
                checksum="2" * 96,
            )
        )
        payload = self.data_dir / "releases" / "@author#1" / "test-123" / "file.zip"
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(b"0123456789")

        torrent_path = (
            self.data_dir / "outbox" / ("0" * 40) / ("1" * 96) / "test.torrent"
        )
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"dummy torrent")

        torrent = TorrentArtifact(
            file_path=Path("/data/releases/@author#1/test-123/file.zip"),
            torrent_path=torrent_path,
            piece_length=16384,
            piece_count=1,
            info_hash="a" * 40,
            torrent_sha256="0" * 64,
            magnet_uri="",
            trackers=(),
        )
        store.record_migrated(
            release,
            file_path=payload,
            sha384="2" * 96,
            sha256="0" * 64,
            torrent=torrent,
        )
        store.block_seeding(
            release.id,
            release.sd_hash,
            code="content_path_conflict",
            error="conflict",
            retry_after=300,
        )

        mock_client._json.return_value = [
            {
                "hash": "a" * 40,
                "save_path": "/downloads/mirror/author/test",
                "content_path": "/downloads/mirror/author/test/file.zip",
            }
        ]

        settings = Settings.from_env(
            {
                "MIRROR_DATA_DIR": str(self.data_dir),
                "MIRROR_QBITTORRENT_ENABLED": "true",
                "MIRROR_QBITTORRENT_USERNAME": "mirror",
                "MIRROR_QBITTORRENT_PASSWORD": "secret",
            }
        )

        stats = relocate_qbit_seeds(settings)
        self.assertEqual(stats["relocated"], 1)
        mock_client.delete.assert_called_once_with("a" * 40, delete_files=False)
        mock_client.add.assert_called_once_with(
            self.data_dir / "outbox" / ("0" * 40) / ("1" * 96) / "test.torrent",
            save_path="/downloads/releases/@author#1/test-123",
            category="guncad-mirror",
            tag="guncad-mirror",
        )
        mock_client.force_start.assert_called_once_with("a" * 40)
        job = store.get(release.id, release.sd_hash)
        self.assertEqual(job.seeding_state, SeedingState.PENDING)
        self.assertIsNone(job.seeding_error_code)

    def test_migration_no_relocate_releases(self) -> None:
        lbry_dir = self.data_dir / "lbry" / "lbrynet"
        lbry_dir.mkdir(parents=True, exist_ok=True)
        db_path = lbry_dir / "lbrynet.sqlite"

        release_dir = self.data_dir / "mirror" / "Author#a" / "Release#b"
        release_dir.mkdir(parents=True, exist_ok=True)
        payload_file = release_dir / "test.zip"
        payload_file.write_bytes(b"content")
        _ = compute_hashes(payload_file)

        rel_id = "5" * 40
        sd_hash = "6" * 96
        (release_dir / "meta.json").write_text(
            json.dumps(
                {
                    "id": rel_id,
                    "name": "No Relocate",
                    "channel": {"handle": "@Author:a"},
                    "sd_hash": sd_hash,
                }
            )
        )

        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE file (stream_hash text, file_name text, download_directory text, status text, saved_file int)"
        )
        conn.execute(
            "CREATE TABLE stream (stream_hash text, sd_hash text, suggested_filename text)"
        )
        fn_hex = "test.zip".encode().hex()
        dd_hex = "/data/mirror/Author#a/Release#b".encode().hex()
        conn.execute("INSERT INTO stream VALUES ('s1', ?, 'test.zip')", (sd_hash,))
        conn.execute(
            f"INSERT INTO file VALUES ('s1', '{fn_hex}', '{dd_hex}', 'stopped', 1)"
        )
        conn.commit()
        conn.close()

        config = MigrationConfig(
            data_dir=self.data_dir,
            relocate_releases=False,
            dry_run=False,
        )
        runner = V1MigrationRunner(config)
        stats = runner.run()

        self.assertEqual(stats.payloads_relocated, 0)
        store = JobStore(self.data_dir / "mirror-state.sqlite3")
        job = store.get(rel_id, sd_hash)
        self.assertEqual(
            job.file_path, Path("/data/mirror/Author#a/Release#b/test.zip")
        )
        self.assertFalse((self.data_dir / "releases").exists())

    def test_migration_upgrades_legacy_job_path(self) -> None:
        lbry_dir = self.data_dir / "lbry" / "lbrynet"
        lbry_dir.mkdir(parents=True, exist_ok=True)
        db_path = lbry_dir / "lbrynet.sqlite"

        release_dir = self.data_dir / "mirror" / "Author#a" / "Release#c"
        release_dir.mkdir(parents=True, exist_ok=True)
        payload_file = release_dir / "upgrade.zip"
        payload_file.write_bytes(b"upgrade payload")

        rel_id = "7" * 40
        sd_hash = "8" * 96
        (release_dir / "meta.json").write_text(
            json.dumps(
                {
                    "id": rel_id,
                    "name": "Upgrade Release",
                    "channel": {"handle": "@Author:a"},
                    "sd_hash": sd_hash,
                }
            )
        )

        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE file (stream_hash text, file_name text, download_directory text, status text, saved_file int)"
        )
        conn.execute(
            "CREATE TABLE stream (stream_hash text, sd_hash text, suggested_filename text)"
        )
        fn_hex = "upgrade.zip".encode().hex()
        dd_hex = "/data/mirror/Author#a/Release#c".encode().hex()
        conn.execute("INSERT INTO stream VALUES ('s1', ?, 'upgrade.zip')", (sd_hash,))
        conn.execute(
            f"INSERT INTO file VALUES ('s1', '{fn_hex}', '{dd_hex}', 'stopped', 1)"
        )
        conn.commit()
        conn.close()

        # Pre-seed the JobStore with an existing legacy record
        store = JobStore(self.data_dir / "mirror-state.sqlite3")
        rel_obj = Release.from_api(
            synthesize_v2_payload(
                release_id=rel_id,
                name="Upgrade Release",
                channel_handle="@Author:a",
                sd_hash=sd_hash,
                size=15,
                checksum="a" * 96,
            )
        )
        dummy_torrent = TorrentArtifact(
            file_path=Path("/data/mirror/Author#a/Release#c/upgrade.zip"),
            torrent_path=self.data_dir / "outbox" / rel_id / sd_hash / "test.torrent",
            piece_length=1048576,
            piece_count=1,
            info_hash="b" * 40,
            torrent_sha256="c" * 64,
            magnet_uri="",
            trackers=(),
        )
        store.record_migrated(
            rel_obj,
            file_path=Path("/data/mirror/Author#a/Release#c/upgrade.zip"),
            sha384="a" * 96,
            sha256="d" * 64,
            torrent=dummy_torrent,
        )

        # Now run migration with relocate_releases=True (default)
        config = MigrationConfig(
            data_dir=self.data_dir,
            relocate_releases=True,
            staging_db=False,
            dry_run=False,
        )
        runner = V1MigrationRunner(config)
        stats = runner.run()

        self.assertEqual(stats.payloads_relocated, 1)
        self.assertEqual(stats.skipped_existing, 1)

        # Verify DB updated and canonical files exist
        job = store.get(rel_id, sd_hash)
        expected_path = Path(
            f"/data/releases/@Author#a/Upgrade-Release-{sd_hash[:12]}/upgrade.zip"
        )
        self.assertEqual(job.file_path, expected_path)
        self.assertTrue(
            (
                self.data_dir
                / "releases"
                / "@Author#a"
                / f"Upgrade-Release-{sd_hash[:12]}"
                / "upgrade.zip"
            ).is_file()
        )
        self.assertTrue(
            (
                self.data_dir
                / "releases"
                / "@Author#a"
                / f"Upgrade-Release-{sd_hash[:12]}"
                / "release.json"
            ).is_file()
        )
