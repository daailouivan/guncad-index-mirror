from __future__ import annotations

import csv
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from guncadmirror.audit import audit_archive, main, write_report
from guncadmirror.models import (
    AcquisitionEvidence,
    AcquisitionTransport,
    PublicationState,
)
from guncadmirror.publisher import OutboxPublisher
from guncadmirror.state import JobStore
from guncadmirror.torrent import create_torrent
from guncadmirror.verification import hash_file

from .helpers import make_release


class ArchiveAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "data"
        self.store = JobStore(self.data_dir / "mirror-state.sqlite3", clock=lambda: 10)
        self.release, self.payload, self.torrent, self.manifest = self._ready_release()
        self.failed_release = make_release(
            b"failed", release_id="c" * 40, sd_hash="d" * 96
        )
        self.store.register(self.failed_release)
        self.store.start_attempt(self.failed_release)
        self.store.mark_failed(
            self.failed_release, RuntimeError("no peers"), retry_backoff=5
        )
        self.excluded_release = make_release(
            b"excluded", release_id="e" * 40, sd_hash="f" * 96
        )
        self.store.register(self.excluded_release)
        self.store.mark_excluded(self.excluded_release, "payload exceeds limit")

    def _ready_release(self):
        content = b"payload"
        release = make_release(content)
        payload = self.data_dir / "releases" / "channel" / "payload.zip"
        payload.parent.mkdir(parents=True)
        payload.write_bytes(content)
        hashes = hash_file(payload)
        torrent_path = (
            self.data_dir
            / "outbox"
            / release.id
            / release.sd_hash
            / f"{hashes.sha384}.torrent"
        )
        torrent = create_torrent(payload, torrent_path, piece_length=16384)
        evidence = AcquisitionEvidence(
            AcquisitionTransport.ODYSEE_CDN,
            source_url=(
                "https://player.odycdn.com/v6/streams/"
                f"{release.id}/{release.sd_hash[:6]}.mp4"
            ),
            lbry_failure="LbryStreamUnavailable: no peers",
        )
        manifest = (
            OutboxPublisher(self.data_dir / "outbox")
            .publish(release, hashes, torrent, evidence)
            .manifest_path
        )
        self.store.register(release)
        self.store.start_attempt(release)
        self.store.mark_verified(
            release,
            file_path=payload,
            sha384=hashes.sha384,
            sha256=hashes.sha256,
        )
        self.store.mark_awaiting_index(release, torrent)
        return release, payload, torrent_path, manifest

    def test_inventories_rehashes_and_writes_machine_readable_reports(self) -> None:
        progress: list[tuple[int, int, Path]] = []
        report = audit_archive(
            self.data_dir,
            rehash_payloads=True,
            progress=lambda *values: progress.append(values),
            now=lambda: datetime(2026, 7, 15, 12, tzinfo=UTC),
        )

        self.assertEqual(report.generated_at, "2026-07-15T12:00:00+00:00")
        self.assertEqual(
            report.job_counts,
            {"awaiting_index": 1, "excluded": 1, "failed": 1},
        )
        self.assertEqual(len(report.artifacts), 1)
        artifact = report.artifacts[0]
        self.assertTrue(artifact.valid)
        self.assertEqual(artifact.seeding_state, "pending")
        self.assertEqual(artifact.publication_state, "pending")
        self.assertEqual(report.seeding_counts, {"pending": 1})
        self.assertEqual(report.publication_counts, {"pending": 1})
        self.assertEqual(report.summary()["seeding_counts"], {"pending": 1})
        self.assertEqual(report.summary()["publication_counts"], {"pending": 1})
        self.assertEqual(artifact.size, len(b"payload"))
        self.assertEqual(artifact.acquisition_transport, "odysee-cdn")
        self.assertIn("no peers", artifact.acquisition_lbry_failure)
        self.assertEqual(progress, [(1, 1, self.payload)])
        self.assertEqual(len(report.failures), 1)
        self.assertEqual(report.failures[0].last_error, "RuntimeError: no peers")
        self.assertEqual(len(report.exclusions), 1)
        self.assertEqual(report.exclusions[0].reason, "payload exceeds limit")
        self.assertEqual(report.issues, ())
        self.assertEqual(
            report.summary()["artifacts"],
            {
                "total": 1,
                "valid": 1,
                "invalid": 0,
                "bytes": 7,
                "unique_payloads": 1,
                "unique_payload_bytes": 7,
                "independently_claimed": 1,
                "descriptor_only": 0,
                "acquisition_transports": {"odysee-cdn": 1},
            },
        )

        output = Path(self.temporary.name) / "reports"
        paths = write_report(report, output)
        self.assertEqual(len(paths), 5)
        self.assertEqual(
            json.loads(paths[0].read_text())["schema"],
            "guncad-mirror-archive-report-v1",
        )
        with paths[1].open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["release_id"], self.release.id)
        self.assertEqual(rows[0]["seeding_state"], "pending")
        self.assertEqual(rows[0]["publication_state"], "pending")
        with paths[3].open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["release_id"], self.excluded_release.id)
        with paths[4].open(newline="") as stream:
            self.assertEqual(
                list(csv.reader(stream))[0], ["release_id", "sd_hash", "message"]
            )

    def test_accepts_pre_fallback_manifest_as_an_lbry_acquisition(self) -> None:
        document = json.loads(self.manifest.read_text())
        del document["acquisition"]
        self.manifest.write_text(json.dumps(document))

        report = audit_archive(self.data_dir)

        self.assertTrue(report.artifacts[0].valid)
        self.assertEqual(report.artifacts[0].acquisition_transport, "lbry")

    def test_validates_and_reports_terminal_publication_receipts(self) -> None:
        btih = self.store.get(self.release.id, self.release.sd_hash).info_hash
        self.store.finish_publication(
            ((self.release.id, self.release.sd_hash),),
            state=PublicationState.PUBLISHED,
            outcome="created",
            canonical=True,
            canonical_sha384=hash_file(self.payload).sha384,
            canonical_btih=btih,
            canonical_torrent_url=f"https://index.example/torrents/{btih}/",
            canonical_magnet_uri=f"magnet:?xt=urn:btih:{btih}",
            winning_release_id=self.release.id,
        )

        report = audit_archive(self.data_dir)

        artifact = report.artifacts[0]
        self.assertTrue(artifact.valid)
        self.assertEqual(artifact.publication_state, "published")
        self.assertEqual(artifact.publication_outcome, "created")
        self.assertTrue(artifact.publication_canonical)
        self.assertEqual(artifact.canonical_btih, btih)
        self.assertEqual(report.publication_counts, {"published": 1})

        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                """
                UPDATE jobs SET canonical_magnet_uri='bad'
                WHERE release_id=? AND sd_hash=?
                """,
                (self.release.id, self.release.sd_hash),
            )
        messages = [issue.message for issue in audit_archive(self.data_dir).issues]
        self.assertIn("magnet URI does not identify the job BTIH", messages)

    def test_validates_qbittorrent_seed_receipts(self) -> None:
        self.store.mark_seed_green(
            self.release.id,
            self.release.sd_hash,
            client_version="v5.2.3",
            observed_state="forcedUP",
            content_path="/downloads/releases/channel/payload.zip",
            dht_nodes=12,
            working_trackers=0,
            recheck_interval=300,
        )

        report = audit_archive(self.data_dir)

        artifact = report.artifacts[0]
        self.assertTrue(artifact.valid)
        self.assertEqual(artifact.seeding_state, "green")
        self.assertEqual(artifact.seeding_client, "qbittorrent")
        self.assertEqual(artifact.seeding_observed_state, "forcedUP")
        self.assertEqual(report.seeding_counts, {"green": 1})

        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                """
                UPDATE jobs SET
                    seeding_observed_state='stoppedUP',
                    seeding_dht_nodes=0,
                    seeding_working_trackers=0,
                    seeding_error_code='stale'
                WHERE release_id=? AND sd_hash=?
                """,
                (self.release.id, self.release.sd_hash),
            )
        messages = [issue.message for issue in audit_archive(self.data_dir).issues]
        self.assertIn("green seed isn't in a qBittorrent upload state", messages)
        self.assertIn("green seed has no peer-discovery path", messages)
        self.assertIn("green seed retains a failure", messages)

        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                "UPDATE jobs SET seeding_state='mystery' WHERE release_id=?",
                (self.release.id,),
            )
        messages = [issue.message for issue in audit_archive(self.data_dir).issues]
        self.assertTrue(any("unknown seeding state" in message for message in messages))

    def test_reports_invalid_publication_states_and_terminal_errors(self) -> None:
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                """
                UPDATE jobs SET publication_state='mystery'
                WHERE release_id=? AND sd_hash=?
                """,
                (self.release.id, self.release.sd_hash),
            )
        report = audit_archive(self.data_dir)
        self.assertTrue(
            any("unknown publication state" in issue.message for issue in report.issues)
        )

        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                """
                UPDATE jobs SET
                    publication_state='conflict', publication_error_code=NULL
                WHERE release_id=? AND sd_hash=?
                """,
                (self.release.id, self.release.sd_hash),
            )
        report = audit_archive(self.data_dir)
        self.assertTrue(
            any("no reason code" in issue.message for issue in report.issues)
        )

    def test_finds_bitrot_manifest_contradictions_and_orphans(self) -> None:
        self.payload.write_bytes(b"payloae")
        self.torrent.write_bytes(self.torrent.read_bytes() + b"corrupt")
        document = json.loads(self.manifest.read_text())
        document["acquisition"] = {
            "transport": "mystery",
            "source_url": None,
            "lbry_failure": None,
        }
        self.manifest.write_text(json.dumps(document))
        orphan = self.data_dir / "outbox" / "orphan" / "stream"
        orphan.mkdir(parents=True)
        (orphan / "manifest.json").write_text("{}")
        (orphan / "payload.torrent").write_bytes(b"torrent")

        report = audit_archive(self.data_dir, rehash_payloads=True)

        self.assertFalse(report.artifacts[0].valid)
        messages = [issue.message for issue in report.issues]
        self.assertIn("SHA-384 mismatch", messages)
        self.assertIn("SHA-256 mismatch", messages)
        self.assertIn("torrent SHA-256 mismatch", messages)
        self.assertIn("unknown acquisition transport 'mystery'", messages)
        self.assertTrue(
            any(message.startswith("orphan manifest") for message in messages)
        )
        self.assertTrue(
            any(message.startswith("orphan torrent") for message in messages)
        )
        self.assertEqual(len(report.orphan_manifests), 1)
        self.assertEqual(len(report.orphan_torrents), 1)

    def test_reports_every_structural_artifact_contradiction(self) -> None:
        self.payload.unlink()
        self.torrent.unlink()
        document = json.loads(self.manifest.read_text())
        document["schema"] = "wrong"
        document["status"] = "published"
        document["release"] = {
            "id": "wrong",
            "name": "wrong",
            "channel_handle": "wrong",
            "url": "https://wrong.example/",
            "url_lbry": "lbry://wrong",
        }
        document["lbry"] = {"sd_hash": "wrong", "claimed_sha384": "wrong"}
        document["artifact"] = {
            "file_name": "wrong",
            "size": 999,
            "sha384": "wrong",
            "sha256": "wrong",
        }
        document["torrent"] = {
            "file_name": "wrong",
            "piece_length": 0,
            "piece_count": 999,
            "btih": "wrong",
            "sha256": "wrong",
            "magnet_uri": "wrong",
        }
        document["acquisition"] = {
            "transport": "odysee-cdn",
            "source_url": "https://evil.example/file",
            "lbry_failure": "",
        }
        self.manifest.write_text(json.dumps(document))
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                "UPDATE jobs SET magnet_uri='wat' WHERE release_id=?",
                (self.release.id,),
            )

        report = audit_archive(self.data_dir)

        messages = {issue.message for issue in report.issues}
        expected = {
            "manifest schema mismatch",
            "manifest status mismatch",
            "release ID mismatch",
            "release name mismatch",
            "channel handle mismatch",
            "release URL mismatch",
            "LBRY URL mismatch",
            "SD hash mismatch",
            "claimed SHA-384 mismatch",
            "artifact size mismatch",
            "manifest SHA-384 mismatch",
            "manifest SHA-256 mismatch",
            "payload file name mismatch",
            "BTIH mismatch",
            "magnet URI mismatch",
            "torrent SHA-256 mismatch",
            "torrent file name mismatch",
            "torrent piece length is invalid",
            "magnet URI does not identify the job BTIH",
            "Odysee source URL identity mismatch",
            "Odysee acquisition has no typed LBRY failure",
        }
        self.assertTrue(expected <= messages)
        self.assertTrue(
            any(message.startswith("payload does not exist") for message in messages)
        )
        self.assertTrue(
            any(message.startswith("torrent does not exist") for message in messages)
        )

    def test_rejects_malformed_manifest_and_acquisition_shapes(self) -> None:
        original = self.manifest.read_text()
        cases = [
            ("not JSON", "cannot read manifest"),
            ("[]", "manifest must be a JSON object"),
            (
                json.dumps(
                    {
                        "schema": "guncad-mirror-publication-v1",
                        "status": "awaiting-index",
                        "acquisition": [],
                    }
                ),
                "manifest acquisition must be an object",
            ),
        ]
        for content, message in cases:
            with self.subTest(message=message):
                self.manifest.write_text(content)
                report = audit_archive(self.data_dir)
                self.assertTrue(
                    any(message in issue.message for issue in report.issues)
                )

        self.manifest.unlink()
        report = audit_archive(self.data_dir)
        self.assertTrue(
            any("cannot read manifest" in issue.message for issue in report.issues)
        )
        self.manifest.write_text(original)

        document = json.loads(original)
        document["acquisition"] = {
            "transport": "lbry",
            "source_url": "https://player.odycdn.com/file",
            "lbry_failure": "failed",
        }
        self.manifest.write_text(json.dumps(document))
        messages = [issue.message for issue in audit_archive(self.data_dir).issues]
        self.assertIn("LBRY acquisition has a source URL", messages)
        self.assertIn("LBRY acquisition has an LBRY failure", messages)

    def test_cli_writes_reports_and_empty_archive_is_valid(self) -> None:
        output = Path(self.temporary.name) / "cli-reports"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(
                [
                    "--data-dir",
                    str(self.data_dir),
                    "--output-dir",
                    str(output),
                    "--rehash",
                ]
            )
        self.assertEqual(status, 0)
        self.assertIn('"integrity_issues": 0', stdout.getvalue())
        self.assertIn("Rehashing 1/1", stderr.getvalue())
        self.assertTrue((output / "archive-artifacts.csv").is_file())

        empty_data = Path(self.temporary.name) / "empty"
        JobStore(empty_data / "mirror-state.sqlite3")
        empty = audit_archive(empty_data)
        self.assertEqual(empty.artifacts, ())
        self.assertEqual(empty.summary()["artifacts"]["total"], 0)

    def test_fails_closed_on_malformed_state_and_cli_errors(self) -> None:
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                """
                UPDATE jobs SET release_json=?, file_path=?, sha384=?
                WHERE release_id=? AND sd_hash=?
                """,
                (
                    "not JSON",
                    "/outside/payload.zip",
                    "wat",
                    self.release.id,
                    self.release.sd_hash,
                ),
            )
            connection.execute(
                "UPDATE jobs SET state='acquiring' WHERE release_id=?",
                (self.failed_release.id,),
            )

        report = audit_archive(self.data_dir)

        self.assertFalse(report.artifacts[0].valid)
        messages = [issue.message for issue in report.issues]
        self.assertTrue(
            any("stored release metadata" in message for message in messages)
        )
        self.assertTrue(any("path escapes" in message for message in messages))
        self.assertTrue(any("lowercase hexadecimal" in message for message in messages))
        self.assertIn("unfinished job state 'acquiring'", messages)

        missing = Path(self.temporary.name) / "missing"
        with self.assertRaises(FileNotFoundError):
            audit_archive(missing)
        self.assertEqual(main(["--data-dir", str(missing)]), 2)

    def test_audit_archive_with_target_prefix(self) -> None:
        hashes = hash_file(self.payload)
        with (
            closing(
                sqlite3.connect(self.data_dir / "mirror-state.sqlite3")
            ) as connection,
            connection,
        ):
            connection.execute(
                """
                UPDATE jobs SET file_path='/data/releases/channel/payload.zip',
                                torrent_path=?
                WHERE release_id=?
                """,
                (
                    f"/data/outbox/{self.release.id}/{self.release.sd_hash}/{hashes.sha384}.torrent",
                    self.release.id,
                ),
            )
        report = audit_archive(self.data_dir, target_prefix=Path("/data"))
        self.assertEqual(len(report.issues), 0)
        self.assertTrue(report.artifacts[0].valid)


if __name__ == "__main__":
    unittest.main()
