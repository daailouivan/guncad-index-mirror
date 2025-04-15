import os
import time
from threading import Thread

from flask import Flask, cli, render_template
from waitress import serve

from . import __main__ as mirror_main
from . import index, stats

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
    context = {"extralog": list(stats.extralog)} | stats.extrastats
    return render_template("index.html", **context)


@app.template_filter()
def humanize_bytes(num):
    for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}iB"
        num /= 1024.0
    return f"{num:.1f} YiB"


def run_flask():
    serve(app, host="0.0.0.0", port="5000", threads=4)


def start():
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
