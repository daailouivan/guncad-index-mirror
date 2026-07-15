from __future__ import annotations

from threading import Thread

from flask import Flask, render_template
from waitress import serve

from .stats import StatsCollector


def create_app(collector: StatsCollector) -> Flask:
    app = Flask(__name__)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600

    @app.route("/")
    def mirror_statistics() -> str:
        return render_template("index.html", **collector.snapshot())

    @app.template_filter()
    def humanize_bytes(num: float) -> str:
        for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
            if abs(num) < 1024.0:
                return f"{num:3.1f} {unit}B"
            num /= 1024.0
        return f"{num:.1f} YiB"

    @app.template_filter()
    def humanize_seconds(num: float) -> str:
        for unit, factor in [
            ("seconds", 60),
            ("minutes", 60),
            ("hours", 24),
            ("days", 7),
            ("weeks", 52),
        ]:
            if abs(num) < factor:
                return f"{num:3.1f} {unit}"
            num /= factor
        return f"{num:.1f} years"

    return app


def start(collector: StatsCollector) -> Thread:
    app = create_app(collector)
    thread = Thread(
        target=serve,
        kwargs={"app": app, "host": "0.0.0.0", "port": 5000, "threads": 4},
        name="mirror-webui",
        daemon=True,
    )
    thread.start()
    return thread
