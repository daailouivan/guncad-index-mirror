from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from guncadmirror.models import JobState, PublicationState, Release, TorrentArtifact
from guncadmirror.settings import Settings
from guncadmirror.state import JobStore
from guncadmirror.webui import create_app, start

from .helpers import release_payload


class WebUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data"
        self.settings = Settings(
            endpoint="https://index.example/api/v2/releases/",
            data_dir=self.root,
        )
        self.store = JobStore(self.settings.state_path)
        disk = SimpleNamespace(percent=25, free=10 * 1024**3)
        network = SimpleNamespace(
            bytes_sent=1,
            bytes_recv=2,
            errout=0,
            errin=0,
            dropout=0,
            dropin=0,
        )
        self.collector = Mock()
        self.collector.settings = self.settings
        self.collector.store = self.store
        self.collector.snapshot.return_value = {
            "version": "test-ref",
            "mirror_state": "Sleeping",
            "mirror_api_endpoint": "https://index.example/api/v2/releases/",
            "mirror_api_max_pages": 2,
            "mirror_max_releases_per_run": None,
            "mirror_lbry_url": "http://127.0.0.1:5279",
            "mirror_lbry_concurrency": 4,
            "mirror_odysee_concurrency": 2,
            "mirror_printables_concurrency": 2,
            "mirror_github_concurrency": 2,
            "mirror_http_concurrency": 2,
            "mirror_torrent_concurrency": 2,
            "mirror_torrent_intake_category": "guncad-intake",
            "mirror_torrent_intake_tag": "guncad-intake",
            "mirror_torrent_download_timeout": 3600,
            "mirror_finalize_concurrency": 2,
            "mirror_qbittorrent_enabled": True,
            "mirror_qbittorrent_url": "http://qbittorrent:8080",
            "mirror_qbittorrent_data_dir": "/downloads",
            "mirror_qbittorrent_timeout": 15,
            "mirror_qbittorrent_ready_timeout": 120,
            "mirror_qbittorrent_recheck_interval": 300,
            "mirror_qbittorrent_category": "guncad-mirror",
            "mirror_qbittorrent_tag": "guncad-mirror",
            "mirror_publish_enabled": False,
            "mirror_publish_url": "",
            "mirror_publish_concurrency": 2,
            "mirror_publish_timeout": 60,
            "mirror_enable_webui": True,
            "mirror_blacklisted_handles": (),
            "mirror_release_max_size": 1024,
            "mirror_min_free_space": 512,
            "mirror_loop_interval": 3600,
            "mirror_cycle_error_interval": 60,
            "mirror_download_timeout": 600,
            "mirror_torrent_piece_length": 1024**2,
            "mirror_torrent_trackers": (),
            "mirror_tracker_policy_url": (
                "https://index.example/api/v2/torrents/tracker-policy/"
            ),
            "mirror_tracker_policy_timeout": 15,
            "mirror_data_dir": "/data",
            "mirror_releases_dir": "/data/releases",
            "mirror_outbox_dir": "/data/outbox",
            "disk_space_used": 123,
            "job_counts": {"awaiting_index": 2, "excluded": 1},
            "seeding_counts": {"pending": 1, "green": 1},
            "publication_counts": {"pending": 2},
            "platform_breakdown": {
                "lbry": {
                    "platform": "lbry",
                    "display_name": "LBRY / Odysee",
                    "total": 2,
                    "pending": 0,
                    "acquiring": 0,
                    "verified": 0,
                    "awaiting_index": 2,
                    "seeding_green": 1,
                    "published": 2,
                    "failed": 0,
                    "excluded": 0,
                    "staged_bytes": 2048,
                    "total_bytes": 2048,
                },
                "printables": {
                    "platform": "printables",
                    "display_name": "Printables",
                    "total": 1,
                    "pending": 0,
                    "acquiring": 0,
                    "verified": 0,
                    "awaiting_index": 0,
                    "seeding_green": 0,
                    "published": 0,
                    "failed": 0,
                    "excluded": 1,
                    "staged_bytes": 0,
                    "total_bytes": 500,
                },
            },
            "platform_totals": {
                "platform": "all",
                "display_name": "All Sources",
                "total": 3,
                "pending": 0,
                "acquiring": 0,
                "verified": 0,
                "awaiting_index": 2,
                "seeding_green": 1,
                "published": 2,
                "failed": 0,
                "excluded": 1,
                "staged_bytes": 2048,
                "total_bytes": 2548,
            },
            "source_file_counts": {"lbry": 2, "printables": 1},
            "source_staged_counts": {"lbry": 2, "printables": 0},
            "known_jobs": 3,
            "activity": None,
            "activities": [],
            "tracker_policy": {
                "enabled": True,
                "endpoint": "https://index.example/api/v2/torrents/tracker-policy/",
                "source": "remote",
                "removals_authoritative": True,
                "desired_trackers": ("udp://tracker.example:80/announce",),
                "enabled_index_trackers": 1,
                "blacklisted_trackers": 0,
                "etag": '"v1"',
                "cached_at": 1,
                "last_checked_at": 2,
                "last_success_at": 2,
                "error_code": None,
                "error": None,
            },
            "psutil_cpu": 1,
            "psutil_mem": 2,
            "psutil_disk": disk,
            "psutil_net": network,
            "github_token_configured": False,
            "github_token_masked": "",
            "extralog": ["event"],
        }

    def _complete_release(
        self,
        *,
        release_id: str = "a" * 40,
        sd_hash: str = "b" * 96,
        name: str = "Release Name",
        channel: str = "@channel:c",
        slug: str = "release:r",
        platform: str = "lbry",
        payload_path: Path | None = None,
        torrent_path: Path | None = None,
    ) -> tuple[Release, bytes, bytes]:
        payload_bytes = f"payload for {name}".encode()
        torrent_bytes = f"torrent for {name}".encode()
        raw = release_payload(
            payload_bytes,
            release_id=release_id,
            sd_hash=sd_hash,
            channel=channel,
            name=name,
        )
        raw["origin"]["slug"] = slug
        raw["origin"]["platform"] = platform
        if platform != "lbry":
            raw["origin"]["external_id"] = release_id
        release = Release.from_api(raw)
        payload_path = payload_path or (
            self.settings.releases_dir / channel / f"{name}.zip"
        )
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.write_bytes(payload_bytes)
        torrent_path = torrent_path or (
            self.settings.outbox_dir / release.id / release.sd_hash / f"{name}.torrent"
        )
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(torrent_bytes)
        self.store.register(release)
        self.store.start_attempt(release)
        self.store.mark_verified(
            release,
            file_path=payload_path,
            sha384="c" * 96,
            sha256="d" * 64,
        )
        self.store.mark_awaiting_index(
            release,
            TorrentArtifact(
                file_path=payload_path,
                torrent_path=torrent_path,
                piece_length=1024**2,
                piece_count=1,
                info_hash="e" * 40,
                torrent_sha256="f" * 64,
                magnet_uri="magnet:?xt=urn:btih:" + "e" * 40,
                trackers=(),
            ),
        )
        return release, payload_bytes, torrent_bytes

    def test_stats_page_and_humanizers_render(self) -> None:
        app = create_app(self.collector)
        response = app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"GunCAD Mirror test-ref", response.data)
        self.assertIn(b"A well regulated Militia", response.data)
        self.assertIn(b"shall not be infringed", response.data)
        self.assertIn(b"Sleeping", response.data)
        self.assertIn(b"Torrents staged", response.data)
        self.assertIn(b"qB seeders green", response.data)
        self.assertIn(b"qBittorrent seeding", response.data)
        self.assertIn(b"1</span> green", response.data)
        self.assertIn(b"http://qbittorrent:8080", response.data)
        self.assertIn(b"excluded by policy", response.data)
        self.assertIn(b"Publication stops at the local outbox", response.data)
        self.assertIn(b"Index publication", response.data)
        self.assertIn(b"2</span> pending", response.data)
        self.assertIn(b"Browse verified files", response.data)
        self.assertIn(b'data-testid="tracker-policy-status"', response.data)
        self.assertNotIn(b'data-testid="tracker-policy-error"', response.data)
        self.assertNotIn(b"LBRY-only mode", response.data)
        self.assertNotIn(b"Assemble Files", response.data)
        self.assertIn(b'data-testid="sources-breakdown"', response.data)
        self.assertIn(b"Ingestion sources", response.data)
        self.assertIn(b"LBRY / Odysee", response.data)
        self.assertIn(b"Printables", response.data)
        self.assertIn(b"Printables acquisition workers", response.data)
        self.assertIn(b"GitHub acquisition workers", response.data)
        self.assertIn(b"Direct HTTP acquisition workers", response.data)
        self.assertIn(b"BitTorrent intake workers", response.data)
        self.assertIn(b"Filter archive", response.data)

        self.collector.snapshot.return_value["tracker_policy"].update(
            {
                "source": "empty",
                "removals_authoritative": False,
                "error_code": "network_error",
                "error": "offline",
            }
        )
        degraded = app.test_client().get("/")
        self.assertIn(b'data-testid="tracker-policy-error"', degraded.data)

        with app.app_context():
            humanize_bytes = app.jinja_env.filters["humanize_bytes"]
            humanize_seconds = app.jinja_env.filters["humanize_seconds"]
            self.assertEqual(humanize_bytes(1024), "1.0 KiB")
            self.assertEqual(humanize_bytes(1024**9), "1024.0 YiB")
            self.assertEqual(humanize_bytes(None), "0.0 B")
            self.assertEqual(humanize_seconds(60), "1.0 minutes")
            self.assertEqual(humanize_seconds(60 * 60 * 24 * 7 * 52), "1.0 years")
            self.assertEqual(humanize_seconds(None), "0.0 seconds")

    def test_active_release_progress_and_lbry_blob_states_render(self) -> None:
        activity = {
            "release_name": "GATALOG",
            "channel_handle": "@Prints.and.the.Revolution:c",
            "release_id": "a" * 40,
            "sd_hash": "b" * 96,
            "phase": "Acquiring from Odysee CDN",
            "transport": "odysee-cdn",
            "completed_bytes": 512,
            "total_bytes": 1024,
            "bytes_per_second": 128,
            "blobs_remaining": None,
        }
        self.collector.snapshot.return_value["activity"] = activity
        self.collector.snapshot.return_value["activities"] = [activity]
        app = create_app(self.collector)

        response = app.test_client().get("/")
        self.assertIn(b"GATALOG", response.data)
        self.assertIn(b"Acquiring from Odysee CDN", response.data)
        self.assertIn(b"50.0%", response.data)
        self.assertIn(b"128.0 B/s", response.data)
        self.assertIn(b"4.0 seconds remaining", response.data)
        self.assertIn(b"1 active job", response.data)

        activity.update(
            {
                "phase": "Acquiring from LBRY",
                "completed_bytes": None,
                "bytes_per_second": None,
                "blobs_remaining": 13,
            }
        )
        response = app.test_client().get("/")
        self.assertIn(b"13 LBRY blobs remaining", response.data)

        activity["blobs_remaining"] = None
        response = app.test_client().get("/")
        self.assertIn(b"Advertised size: 1.0 KiB", response.data)

        second = dict(activity, release_name="Second release", release_id="c" * 40)
        self.collector.snapshot.return_value["activities"] = [activity, second]
        response = app.test_client().get("/")
        self.assertIn(b"2 active jobs", response.data)
        self.assertIn(b"Second release", response.data)

    def test_archive_search_pagination_and_downloads(self) -> None:
        first, first_payload, first_torrent = self._complete_release(
            name="Alpha Jig",
            channel="@Maker:a",
            slug="alpha-jig:a",
        )
        second, _, _ = self._complete_release(
            release_id="c" * 40,
            sd_hash="d" * 96,
            name="Beta Fixture",
            channel="@Other:b",
            slug="beta-fixture:b",
        )
        app = create_app(self.collector)
        client = app.test_client()

        response = client.get("/archive")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Alpha Jig", response.data)
        self.assertIn(b"Beta Fixture", response.data)
        self.assertIn(b"@Maker:a", response.data)
        self.assertIn(b"alpha-jig:a", response.data)
        self.assertIn(b"Downloads are unauthenticated", response.data)
        self.assertIn(b"Download file", response.data)
        self.assertIn(b"Download torrent", response.data)
        self.assertIn(b"magnet:?xt=urn:btih:", response.data)

        response = client.get("/archive?q=maker+alpha-jig")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Alpha Jig", response.data)
        self.assertNotIn(b"Beta Fixture", response.data)

        with patch("guncadmirror.webui.ARCHIVE_PAGE_SIZE", 1):
            response = client.get("/archive?page=2")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Alpha Jig", response.data)
        self.assertIn(b"Beta Fixture", response.data)
        self.assertIn(b"Page 2 of 2", response.data)

        payload = client.get(
            f"/archive/{first.id}/{first.sd_hash}/payload",
        )
        self.assertEqual(payload.status_code, 200)
        self.assertEqual(payload.data, first_payload)
        self.assertIn("attachment", payload.headers["Content-Disposition"])
        self.assertEqual(payload.headers["X-Content-Type-Options"], "nosniff")
        payload.close()

        partial = client.get(
            f"/archive/{first.id}/{first.sd_hash}/payload",
            headers={"Range": "bytes=0-6"},
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.data, first_payload[:7])
        partial.close()

        torrent = client.get(
            f"/archive/{first.id}/{first.sd_hash}/torrent",
        )
        self.assertEqual(torrent.status_code, 200)
        self.assertEqual(torrent.data, first_torrent)
        self.assertEqual(torrent.mimetype, "application/x-bittorrent")
        torrent.close()

        self.assertEqual(
            client.get(f"/archive/{second.id}/bad/payload").status_code,
            404,
        )

        self.store.finish_publication(
            ((first.id, first.sd_hash),),
            state=PublicationState.DUPLICATE,
            outcome="artifact_duplicate",
            canonical=False,
            canonical_sha384="c" * 96,
            canonical_btih="f" * 40,
            canonical_torrent_url="https://index.example/torrents/f/",
            canonical_magnet_uri="magnet:?xt=urn:btih:" + "f" * 40,
            winning_release_id=second.id,
        )
        response = client.get("/archive?q=Alpha")
        self.assertIn(b"Index publication: duplicate", response.data)
        self.assertIn(b"Index torrent", response.data)
        self.assertIn(b"Canonical magnet", response.data)

    def test_archive_multi_source_filtering_and_downloads(self) -> None:
        lbry_rel, _, _ = self._complete_release(
            name="LBRY Part",
            channel="@DefCad:1",
            platform="lbry",
        )
        print_rel, print_payload, print_torrent = self._complete_release(
            release_id="printables-12345",
            name="Printables Frame",
            channel="@Ivan:2",
            platform="printables",
        )
        gh_rel, _, _ = self._complete_release(
            release_id="github-org-repo-v1",
            name="GitHub Receiver",
            channel="@AWCY:3",
            platform="github",
        )
        app = create_app(self.collector)
        client = app.test_client()

        # Filtering by platform=printables
        response = client.get("/archive?platform=printables")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Printables Frame", response.data)
        self.assertNotIn(b"LBRY Part", response.data)
        self.assertNotIn(b"GitHub Receiver", response.data)
        self.assertIn(
            b'<option value="printables" selected>Printables</option>', response.data
        )

        # Non-LBRY artifact downloads (Printables)
        payload = client.get(f"/archive/{print_rel.id}/{print_rel.sd_hash}/payload")
        self.assertEqual(payload.status_code, 200)
        self.assertEqual(payload.data, print_payload)
        self.assertIn("attachment", payload.headers["Content-Disposition"])
        payload.close()

        torrent = client.get(f"/archive/{print_rel.id}/{print_rel.sd_hash}/torrent")
        self.assertEqual(torrent.status_code, 200)
        self.assertEqual(torrent.data, print_torrent)
        self.assertEqual(torrent.mimetype, "application/x-bittorrent")
        torrent.close()

    def test_enabled_publication_and_terminal_counts_render_without_a_token(
        self,
    ) -> None:
        snapshot = self.collector.snapshot.return_value
        snapshot.update(
            {
                "mirror_publish_enabled": True,
                "mirror_publish_url": "https://index.example/api/v2/torrents/publish/",
                "mirror_publish_concurrency": 3,
                "mirror_publish_timeout": 20,
                "publication_counts": {
                    "published": 4,
                    "duplicate": 2,
                    "rejected": 1,
                    "conflict": 1,
                },
            }
        )

        response = create_app(self.collector).test_client().get("/")

        self.assertIn(b"Index publication is enabled", response.data)
        self.assertIn(b"6</span>", response.data)
        self.assertIn(b"operator attention", response.data)
        self.assertIn(b"Publication workers", response.data)
        self.assertNotIn(b"secret", response.data)

    def test_archive_downloads_reject_unfinished_missing_and_escaped_paths(
        self,
    ) -> None:
        pending_raw = release_payload(release_id="1" * 40, sd_hash="1" * 96)
        pending = Release.from_api(pending_raw)
        self.store.register(pending)

        outside = Path(self.temporary.name) / "outside.zip"
        escaped, _, _ = self._complete_release(
            release_id="2" * 40,
            sd_hash="2" * 96,
            name="Escaped",
            payload_path=outside,
        )
        missing, _, _ = self._complete_release(
            release_id="3" * 40,
            sd_hash="3" * 96,
            name="Missing",
        )
        missing_job = self.store.get(missing.id, missing.sd_hash)
        self.assertIsNotNone(missing_job.file_path)
        missing_job.file_path.unlink()

        outside_torrent = Path(self.temporary.name) / "outside.torrent"
        escaped_torrent, _, _ = self._complete_release(
            release_id="4" * 40,
            sd_hash="4" * 96,
            name="Escaped Torrent",
            torrent_path=outside_torrent,
        )
        app = create_app(self.collector)
        client = app.test_client()

        self.assertEqual(
            client.get(f"/archive/{pending.id}/{pending.sd_hash}/payload").status_code,
            404,
        )
        self.assertEqual(
            client.get(f"/archive/{escaped.id}/{escaped.sd_hash}/payload").status_code,
            404,
        )
        self.assertEqual(
            client.get(f"/archive/{missing.id}/{missing.sd_hash}/payload").status_code,
            404,
        )
        self.assertEqual(
            client.get(
                f"/archive/{escaped_torrent.id}/{escaped_torrent.sd_hash}/torrent"
            ).status_code,
            404,
        )

    def test_archive_rejects_overlong_queries(self) -> None:
        app = create_app(self.collector)
        response = app.test_client().get("/archive?q=" + "x" * 201)
        self.assertEqual(response.status_code, 400)

    @patch("guncadmirror.webui.Thread")
    @patch("guncadmirror.webui.serve")
    def test_start_launches_waitress_daemon_thread(
        self, serve: Mock, thread_class: Mock
    ) -> None:
        thread = thread_class.return_value
        self.assertIs(start(self.collector), thread)
        kwargs = thread_class.call_args.kwargs
        self.assertEqual(kwargs["name"], "mirror-webui")
        self.assertTrue(kwargs["daemon"])
        self.assertIs(kwargs["target"], serve)
        self.assertEqual(kwargs["kwargs"]["port"], 5000)
        self.assertEqual(kwargs["kwargs"]["threads"], 8)
        thread.start.assert_called_once_with()

    def test_update_github_token_endpoint(self) -> None:
        app = create_app(self.collector)
        client = app.test_client()

        # Insert a failed github job into the real store to test retry on token update
        gh_raw = release_payload(b"github", release_id="github-123", sd_hash="a" * 96)
        gh_raw["origin"]["platform"] = "github"
        gh_rel = Release.from_api(gh_raw)
        self.store.register(gh_rel)
        self.store.start_attempt(gh_rel)
        self.store.mark_failed(gh_rel, RuntimeError("Rate limit hit"), retry_backoff=2.0)

        # Post token update
        response = client.post(
            "/settings/github-token",
            data={"github_token": "ghp_mocktoken12345678"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/?token_saved=1", response.headers["Location"])

        # Check real store updated and failed job was reset to pending
        self.assertEqual(self.store.get_setting("github_token"), "ghp_mocktoken12345678")
        job = self.store.get("github-123", "a" * 96)
        self.assertEqual(job.state, JobState.PENDING)

        # Test index page rendering with saved token banner and configured status
        self.collector.snapshot.return_value["github_token_configured"] = True
        self.collector.snapshot.return_value["github_token_masked"] = "ghp_...5678"

        index_resp = client.get("/?token_saved=1")
        self.assertEqual(index_resp.status_code, 200)
        self.assertIn(b"GitHub Token updated successfully", index_resp.data)
        self.assertIn(b"Authenticated (5,000 req/hr)", index_resp.data)
        self.assertIn(b"ghp_...5678", index_resp.data)

    def test_api_entries_and_jobs_retry_endpoints(self) -> None:
        app = create_app(self.collector)
        client = app.test_client()

        # Check index renders modal dialog and trigger attributes
        index_resp = client.get("/")
        self.assertEqual(index_resp.status_code, 200)
        self.assertIn(b'id="category-modal"', index_resp.data)
        self.assertIn(b'data-modal-trigger', index_resp.data)

        # Register and fail a job
        rel_id = "c" * 40
        sd_hash = "f" * 96
        rel = Release.from_api(
            release_payload(b"model data", release_id=rel_id, sd_hash=sd_hash, name="Test Print")
        )
        self.store.register(rel)
        self.store.start_attempt(rel)
        self.store.mark_failed(rel, RuntimeError("Server 500 error"), retry_backoff=2.0)

        # Query api entries
        resp = client.get("/api/entries?section=pipeline&category=failed")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["section"], "pipeline")
        self.assertEqual(data["category"], "failed")
        self.assertEqual(len(data["entries"]), 1)
        self.assertEqual(data["entries"][0]["release_id"], rel_id)
        self.assertIn("Server 500 error", data["entries"][0]["last_error"])

        # Retry single job via POST /api/jobs/retry
        retry_resp = client.post(
            "/api/jobs/retry",
            json={"release_id": rel_id, "sd_hash": sd_hash},
        )
        self.assertEqual(retry_resp.status_code, 200)
        retry_data = retry_resp.get_json()
        self.assertTrue(retry_data["ok"])
        self.assertEqual(retry_data["retried"], 1)

        # Check job is now pending
        job = self.store.get(rel_id, sd_hash)
        self.assertEqual(job.state, JobState.PENDING)

        # Mark failed again to test bulk retry
        self.store.start_attempt(rel)
        self.store.mark_failed(rel, RuntimeError("Second error"), retry_backoff=2.0)

        bulk_retry_resp = client.post("/api/jobs/retry", json={})
        self.assertEqual(bulk_retry_resp.status_code, 200)
        bulk_data = bulk_retry_resp.get_json()
        self.assertTrue(bulk_data["ok"])
        self.assertEqual(bulk_data["retried"], 1)


if __name__ == "__main__":
    unittest.main()
