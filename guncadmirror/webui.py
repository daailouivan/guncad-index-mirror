from __future__ import annotations

from math import ceil
from pathlib import Path
from threading import Thread

from flask import Flask, Response, abort, redirect, render_template, request, send_file
from waitress import serve

from .models import RELEASE_ID_RE, SHA384_RE, JobState
from .paths import ensure_within
from .stats import StatsCollector

ARCHIVE_PAGE_SIZE = 50
MAX_ARCHIVE_QUERY_LENGTH = 200


def create_app(collector: StatsCollector) -> Flask:
    app = Flask(__name__)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600

    @app.route("/")
    def mirror_statistics() -> str:
        context = collector.snapshot()
        context["token_saved"] = request.args.get("token_saved") == "1"
        return render_template("index.html", **context)

    @app.route("/settings/github-token", methods=["POST"])
    def update_github_token() -> Response:
        raw_token = request.form.get("github_token", "").strip()
        collector.store.set_setting("github_token", raw_token)
        if raw_token:
            retried = collector.store.retry_failed_jobs(platform="github")
            if retried > 0:
                collector.events.append(
                    f"Reset {retried} rate-limited GitHub releases for immediate retry with updated token"
                )
        collector.collect()
        return redirect("/?token_saved=1")

    @app.route("/archive")
    def archive_browser() -> str:
        query = request.args.get("q", "").strip()
        platform_filter = request.args.get("platform", "").strip().lower() or None
        if len(query) > MAX_ARCHIVE_QUERY_LENGTH:
            abort(400, "archive query is too long")
        page = request.args.get("page", default=1, type=int) or 1
        page = max(page, 1)
        entries, total = collector.store.search_archive(
            query,
            platform=platform_filter,
            limit=ARCHIVE_PAGE_SIZE,
            offset=(page - 1) * ARCHIVE_PAGE_SIZE,
        )
        page_count = max(1, ceil(total / ARCHIVE_PAGE_SIZE))
        if page > page_count:
            page = page_count
            entries, total = collector.store.search_archive(
                query,
                platform=platform_filter,
                limit=ARCHIVE_PAGE_SIZE,
                offset=(page - 1) * ARCHIVE_PAGE_SIZE,
            )
        context = collector.snapshot()
        context.update(
            {
                "archive_entries": entries,
                "archive_query": query,
                "archive_platform": platform_filter or "",
                "archive_total": total,
                "archive_page": page,
                "archive_page_count": page_count,
            }
        )
        return render_template("archive.html", **context)

    @app.route("/archive/<release_id>/<sd_hash>/payload")
    def download_payload(release_id: str, sd_hash: str) -> Response:
        return _send_artifact(
            collector,
            release_id,
            sd_hash,
            field="file_path",
            root=collector.settings.data_dir,
        )

    @app.route("/archive/<release_id>/<sd_hash>/torrent")
    def download_torrent(release_id: str, sd_hash: str) -> Response:
        return _send_artifact(
            collector,
            release_id,
            sd_hash,
            field="torrent_path",
            root=collector.settings.outbox_dir,
            mimetype="application/x-bittorrent",
        )

    @app.template_filter()
    def humanize_bytes(num: float | None) -> str:
        if num is None:
            return "0.0 B"
        try:
            val = float(num)
        except (ValueError, TypeError):
            return "0.0 B"
        for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
            if abs(val) < 1024.0:
                return f"{val:3.1f} {unit}B"
            val /= 1024.0
        return f"{val:.1f} YiB"

    @app.template_filter()
    def humanize_seconds(num: float | None) -> str:
        if num is None:
            return "0.0 seconds"
        try:
            val = float(num)
        except (ValueError, TypeError):
            return "0.0 seconds"
        for unit, factor in [
            ("seconds", 60),
            ("minutes", 60),
            ("hours", 24),
            ("days", 7),
            ("weeks", 52),
        ]:
            if abs(val) < factor:
                return f"{val:3.1f} {unit}"
            val /= factor
        return f"{val:.1f} years"

    return app


def _send_artifact(
    collector: StatsCollector,
    release_id: str,
    sd_hash: str,
    *,
    field: str,
    root: Path,
    mimetype: str | None = None,
) -> Response:
    if not RELEASE_ID_RE.fullmatch(release_id) or not SHA384_RE.fullmatch(sd_hash):
        abort(404)
    try:
        job = collector.store.get(release_id, sd_hash)
    except KeyError:
        abort(404)
    if job.state is not JobState.AWAITING_INDEX:
        abort(404)
    raw_path = getattr(job, field)
    if raw_path is None:
        abort(404)
    try:
        path = ensure_within(root, raw_path)
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)
    response = send_file(
        path,
        as_attachment=True,
        download_name=path.name,
        conditional=True,
        max_age=0,
        mimetype=mimetype,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def start(collector: StatsCollector) -> Thread:
    app = create_app(collector)
    thread = Thread(
        target=serve,
        kwargs={"app": app, "host": "0.0.0.0", "port": 5000, "threads": 8},
        name="mirror-webui",
        daemon=True,
    )
    thread.start()
    return thread
