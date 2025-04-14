from flask import Flask, render_template, cli
from threading import Thread
import time



app = Flask(__name__)
flask_thread = Thread(target=app.run(debug=True))
flask_thread.daemon = True
# This is a dirty nasty hack to disable showing the banner that gives a big
# "dev server only" warning. We don't need that because:
#    A. This is an internal-only process
#    B. We state as much in the readme; and
#    C. We're going to warn the user ourselves
cli.show_server_banner = lambda *_: None

@app.route("/")
def index():
    stats = {"cpu": 42, "mem": 68}
    return render_template("index.html", **stats)


def start():
    flask_thread.start()
