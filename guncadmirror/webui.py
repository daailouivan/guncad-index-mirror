import os
import time
from threading import Thread

import psutil
from flask import Flask, cli, render_template
from waitress import serve

from . import __main__ as mirror_main
from . import index

extrastats = {}

app = Flask(__name__)
# This is a dirty nasty hack to disable showing the banner that gives a big
# "dev server only" warning. We don't need that because:
#    A. This is an internal-only process
#    B. We state as much in the readme; and
#    C. We're going to warn the user ourselves
cli.show_server_banner = lambda *_: None


@app.route("/")
def stats():
    stats = {
        "psutil_cpu": psutil.cpu_percent(interval=0.2),
        "psutil_mem": psutil.virtual_memory().percent,
        "psutil_net": psutil.net_io_counters(),
        "psutil_disk": psutil.disk_usage("/data"),
        "seen_sd_hashes": len(index.seen_sd_hashes.cache),
        "disk_space_used": get_dir_size("/data"),
    } | extrastats
    return render_template("index.html", **stats)


@app.template_filter()
def humanize_bytes(num):
    for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}iB"
        num /= 1024.0
    return f"{num:.1f} YiB"


def get_dir_size(path):
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except FileNotFoundError:
                pass  # File might vanish during the walk
    return total


def run_flask():
    serve(app, host="0.0.0.0", port="5000", threads=4)


def start():
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
