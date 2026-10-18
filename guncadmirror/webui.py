from __future__ import annotations

import shutil
import subprocess
from math import ceil
from pathlib import Path
from threading import Thread

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
)
from waitress import serve

from .models import RELEASE_ID_RE, SHA384_RE, JobState
from .paths import ensure_within
from .stats import StatsCollector

ARCHIVE_PAGE_SIZE = 50
MAX_ARCHIVE_QUERY_LENGTH = 200


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


def create_app(collector: StatsCollector) -> Flask:
    app = Flask(__name__)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600
    app.template_filter("humanize_bytes")(humanize_bytes)
    app.template_filter("humanize_seconds")(humanize_seconds)

    @app.context_processor
    def inject_cachebuster() -> dict[str, str]:
        css_path = Path(app.static_folder or "") / "styles.css"
        if css_path.exists():
            return {"cachebuster": f"?v={int(css_path.stat().st_mtime)}"}
        return {"cachebuster": ""}

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

    @app.route("/api/features", methods=["GET"])
    def get_features() -> Response:
        return jsonify(
            {
                "ok": True,
                "features": {
                    "catalog_details": collector.is_feature_enabled("catalog_details"),
                    "archive_inspector": collector.is_feature_enabled(
                        "archive_inspector"
                    ),
                },
            }
        )

    @app.route("/api/features", methods=["POST"])
    def update_features() -> Response:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400

        valid_features = {"catalog_details", "archive_inspector"}
        updates: dict[str, bool] = {}
        if "feature" in payload:
            feat = str(payload["feature"]).strip()
            if feat not in valid_features:
                return jsonify({"ok": False, "error": f"Unknown feature: {feat}"}), 400
            updates[feat] = bool(payload.get("enabled", True))
        elif "features" in payload and isinstance(payload["features"], dict):
            for feat, val in payload["features"].items():
                if feat in valid_features:
                    updates[feat] = bool(val)

        if not updates:
            return (
                jsonify({"ok": False, "error": "No valid feature updates specified"}),
                400,
            )

        for feat, val in updates.items():
            collector.set_feature_enabled(feat, val)
            collector.events.append(f"Feature '{feat}' set to {val}")

        collector.collect()
        return jsonify(
            {
                "ok": True,
                "message": "Features updated successfully",
                "features": {
                    "catalog_details": collector.is_feature_enabled("catalog_details"),
                    "archive_inspector": collector.is_feature_enabled(
                        "archive_inspector"
                    ),
                },
            }
        )

    @app.route("/api/entries")
    def api_category_entries() -> Response:
        section = request.args.get("section", "pipeline").strip().lower()
        category = request.args.get("category", "all").strip().lower()
        platform_filter = request.args.get("platform", "").strip().lower() or None
        query = request.args.get("q", "").strip()
        if len(query) > MAX_ARCHIVE_QUERY_LENGTH:
            abort(400, "query is too long")
        limit = request.args.get("limit", default=50, type=int) or 50
        limit = max(1, min(limit, 200))
        page = request.args.get("page", default=1, type=int) or 1
        page = max(1, page)
        offset = (page - 1) * limit

        entries, total = collector.store.get_category_entries(
            section=section,
            category=category,
            platform=platform_filter,
            query=query,
            limit=limit,
            offset=offset,
        )
        page_count = max(1, ceil(total / limit)) if total > 0 else 1

        activities_map = collector.get_activities()
        formatted_entries = []
        for entry in entries:
            item = dict(entry)
            item["size_human"] = humanize_bytes(item.get("payload_size"))
            rel_id = item.get("release_id")
            sd_h = item.get("sd_hash")
            act = activities_map.get((rel_id, sd_h))
            if act:
                completed = act.get("completed_bytes")
                total_b = act.get("total_bytes")
                rate = act.get("bytes_per_second")
                pct = None
                try:
                    if (
                        completed is not None
                        and total_b is not None
                        and float(total_b) > 0
                    ):
                        pct = round((float(completed) / float(total_b)) * 100, 1)
                except (TypeError, ValueError):
                    pct = None
                eta_sec = None
                try:
                    if (
                        rate
                        and float(rate) > 0
                        and total_b is not None
                        and completed is not None
                        and float(total_b) > float(completed)
                    ):
                        eta_sec = (float(total_b) - float(completed)) / float(rate)
                except (TypeError, ValueError):
                    eta_sec = None

                completed_h = (
                    humanize_bytes(completed)
                    if isinstance(completed, (int, float))
                    else None
                )
                total_h = (
                    humanize_bytes(total_b)
                    if isinstance(total_b, (int, float))
                    else None
                )
                speed_h = (
                    f"{humanize_bytes(rate)}/s"
                    if isinstance(rate, (int, float))
                    else None
                )
                eta_h = (
                    humanize_seconds(eta_sec)
                    if isinstance(eta_sec, (int, float))
                    else None
                )

                item["activity"] = {
                    "phase": act.get("phase")
                    if isinstance(act.get("phase"), str)
                    else None,
                    "transport": act.get("transport")
                    if isinstance(act.get("transport"), str)
                    else None,
                    "progress_pct": pct,
                    "completed_bytes": completed
                    if isinstance(completed, (int, float))
                    else None,
                    "total_bytes": total_b
                    if isinstance(total_b, (int, float))
                    else None,
                    "completed_human": completed_h,
                    "total_human": total_h,
                    "speed_human": speed_h,
                    "eta_human": eta_h,
                    "blobs_remaining": act.get("blobs_remaining")
                    if isinstance(act.get("blobs_remaining"), int)
                    else None,
                }
            else:
                item["activity"] = None
            if (
                item.get("has_payload")
                and item.get("state") == JobState.AWAITING_INDEX.value
            ):
                item["payload_url"] = (
                    f"/archive/{item['release_id']}/{item['sd_hash']}/payload"
                )
            else:
                item["payload_url"] = None
            if item.get("has_payload"):
                item["location_url"] = (
                    f"/api/entries/{item['release_id']}/{item['sd_hash']}/location"
                )
            else:
                item["location_url"] = None
            if (
                item.get("has_torrent")
                and item.get("state") == JobState.AWAITING_INDEX.value
            ):
                item["torrent_url"] = (
                    f"/archive/{item['release_id']}/{item['sd_hash']}/torrent"
                )
            else:
                item["torrent_url"] = None
            formatted_entries.append(item)

        return jsonify(
            {
                "section": section,
                "category": category,
                "platform": platform_filter,
                "query": query,
                "total": total,
                "page": page,
                "page_count": page_count,
                "limit": limit,
                "entries": formatted_entries,
            }
        )

    @app.route("/api/jobs/retry", methods=["POST"])
    def api_retry_jobs() -> Response:
        data = request.get_json(silent=True) or request.form
        release_id = data.get("release_id", "").strip()
        sd_hash = data.get("sd_hash", "").strip()
        platform_filter = data.get("platform", "").strip().lower() or None

        if release_id and sd_hash:
            success = collector.store.retry_job(release_id, sd_hash)
            retried = 1 if success else 0
            if success:
                collector.events.append(
                    f"Reset job {release_id} ({sd_hash[:12]}) for retry"
                )
        else:
            retried = collector.store.retry_failed_jobs(platform=platform_filter)
            if retried > 0:
                target = f"{platform_filter} " if platform_filter else ""
                collector.events.append(
                    f"Reset {retried} failed {target}jobs for immediate retry"
                )
        collector.collect()
        return jsonify({"ok": True, "retried": retried})

    @app.route("/api/jobs/exclude", methods=["POST"])
    def api_exclude_job() -> Response:
        data = request.get_json(silent=True) or request.form
        release_id = data.get("release_id", "").strip()
        sd_hash = data.get("sd_hash", "").strip()
        reason = (data.get("reason", "") or "").strip() or "Non-model software"

        if not release_id or not sd_hash:
            abort(400, "release_id and sd_hash are required")

        success = collector.store.exclude_job(release_id, sd_hash, reason=reason)
        if success:
            collector.events.append(f"Excluded {release_id} ({sd_hash[:12]}): {reason}")
            collector.collect()
        return jsonify({"ok": success, "excluded": 1 if success else 0})

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

    @app.route("/api/entries/<release_id>/<sd_hash>/details")
    def entry_details(release_id: str, sd_hash: str) -> Response:
        if not RELEASE_ID_RE.fullmatch(release_id) or not SHA384_RE.fullmatch(sd_hash):
            abort(404)
        if not collector.is_feature_enabled("catalog_details"):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            "Catalog details feature is disabled. Enable it in Resource Profile "
                            "& Optional Features to view rich metadata."
                        ),
                        "feature_disabled": True,
                    }
                ),
                403,
            )
        detail = collector.store.get_release_detail(release_id, sd_hash)
        if detail is None:
            abort(404)

        if detail.get("file_path") and collector.settings.host_data_dir:
            try:
                p = Path(detail["file_path"])
                rel = p.resolve().relative_to(collector.settings.data_dir.resolve())
                host_p = Path(collector.settings.host_data_dir) / rel
                detail["host_file_path"] = str(host_p)
                detail["host_folder_path"] = str(host_p.parent)
            except (ValueError, TypeError):
                detail["host_file_path"] = detail.get("file_path")
                detail["host_folder_path"] = detail.get("folder_path")
        else:
            detail["host_file_path"] = detail.get("file_path")
            detail["host_folder_path"] = detail.get("folder_path")

        detail["payload_url"] = (
            f"/archive/{release_id}/{sd_hash}/payload"
            if detail.get("file_path")
            and detail.get("state") == JobState.AWAITING_INDEX.value
            else None
        )
        detail["torrent_url"] = (
            f"/archive/{release_id}/{sd_hash}/torrent"
            if detail.get("torrent_path")
            and detail.get("state") == JobState.AWAITING_INDEX.value
            else None
        )

        return jsonify({"ok": True, "detail": detail})

    @app.route("/api/entries/<release_id>/<sd_hash>/location")
    def entry_location(release_id: str, sd_hash: str) -> Response:
        if not RELEASE_ID_RE.fullmatch(release_id) or not SHA384_RE.fullmatch(sd_hash):
            abort(404)
        try:
            job = collector.store.get(release_id, sd_hash)
        except KeyError:
            abort(404)
        if job.file_path is None:
            abort(404)
        try:
            path = ensure_within(collector.settings.data_dir, job.file_path)
        except ValueError:
            abort(404)

        folder = path.parent
        file_exists = path.is_file()
        folder_exists = folder.is_dir()

        host_folder = str(folder)
        host_file = str(path)
        if collector.settings.host_data_dir:
            try:
                rel = path.resolve().relative_to(collector.settings.data_dir.resolve())
                host_p = Path(collector.settings.host_data_dir) / rel
                host_file = str(host_p)
                host_folder = str(host_p.parent)
            except (ValueError, TypeError):
                pass

        payload_url = None
        if file_exists and job.state == JobState.AWAITING_INDEX:
            payload_url = f"/archive/{release_id}/{sd_hash}/payload"

        torrent_url = None
        if job.torrent_path and job.state == JobState.AWAITING_INDEX:
            torrent_url = f"/archive/{release_id}/{sd_hash}/torrent"

        return jsonify(
            {
                "ok": True,
                "release_id": release_id,
                "sd_hash": sd_hash,
                "file_name": path.name,
                "file_path": str(path),
                "folder_path": str(folder),
                "host_file_path": host_file,
                "host_folder_path": host_folder,
                "exists": file_exists,
                "folder_exists": folder_exists,
                "payload_url": payload_url,
                "torrent_url": torrent_url,
                "seeding_state": job.seeding_state.value if job.seeding_state else None,
                "caution_warning": (
                    "Active BitTorrent Seeding Directory: Modifying, renaming, moving, "
                    "unzipping in-place, or deleting files in this directory will corrupt "
                    "torrent piece hashes and disrupt seeding."
                ),
                "preferred_workflow": (
                    "Download an additional copy to ensure integrity of the seeding status "
                    "before unzipping or modifying files."
                ),
            }
        )

    @app.route("/api/entries/<release_id>/<sd_hash>/open-location", methods=["POST"])
    def entry_open_location(release_id: str, sd_hash: str) -> Response:
        if not RELEASE_ID_RE.fullmatch(release_id) or not SHA384_RE.fullmatch(sd_hash):
            abort(404)
        try:
            job = collector.store.get(release_id, sd_hash)
        except KeyError:
            abort(404)
        if job.file_path is None:
            abort(404)
        try:
            path = ensure_within(collector.settings.data_dir, job.file_path)
        except ValueError:
            abort(404)

        folder = path.parent
        opened, message = _try_open_local_path(folder, path)
        return jsonify(
            {
                "ok": True,
                "opened": opened,
                "folder_path": str(folder),
                "file_path": str(path),
                "message": (
                    "File manager opened"
                    if opened
                    else (message or "Could not open file manager")
                ),
            }
        )

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


def _try_open_local_path(
    folder_path: Path, file_path: Path | None = None
) -> tuple[bool, str | None]:
    if not folder_path.exists():
        return False, f"Directory does not exist on disk: {folder_path}"

    opener = None
    args: list[str] = []
    if shutil.which("xdg-open"):
        opener = "xdg-open"
        args = ["xdg-open", str(folder_path)]
    elif shutil.which("gio"):
        opener = "gio"
        args = ["gio", "open", str(folder_path)]
    elif shutil.which("open"):
        opener = "open"
        if file_path and file_path.exists():
            args = ["open", "-R", str(file_path)]
        else:
            args = ["open", str(folder_path)]
    elif shutil.which("explorer.exe"):
        opener = "explorer.exe"
        args = ["explorer.exe", str(folder_path)]

    if not opener:
        return (
            False,
            "No desktop file manager opener (e.g. xdg-open) is available on this system.",
        )

    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True, None
    except Exception as exc:
        return False, f"Failed to launch file manager: {exc}"
