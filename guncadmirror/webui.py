import os
import random
import time
from threading import Thread

from flask import Flask, cli, render_template
from waitress import serve

from . import __main__ as mirror_main
from . import index, settings, stats

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600
# This is a dirty nasty hack to disable showing the banner that gives a big
# "dev server only" warning. We don't need that because:
#    A. This is an internal-only process
#    B. We state as much in the readme; and
#    C. We're going to warn the user ourselves
cli.show_server_banner = lambda *_: None


@app.route("/")
def mirror_statistics():
    context = {
        "extralog": list(stats.extralog),
        "cachebuster": settings.cachebuster,
    } | stats.extrastats
    return render_template("index.html", **context)


@app.template_filter()
def humanize_bytes(num):
    for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}B"
        num /= 1024.0
    return f"{num:.1f} YiB"


@app.template_filter()
def humanize_seconds(num):
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


def run_flask():
    serve(app, host="0.0.0.0", port="5000", threads=4)


def start():
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    stats.log("Started web UI thread", stdout=True)
