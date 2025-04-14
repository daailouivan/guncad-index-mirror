import os
import time
from threading import Thread

import psutil

from . import index

extrastats = {}


def run_stats():
    while True:
        extrastats["psutil_cpu"] = psutil.cpu_percent(interval=0.2)
        extrastats["psutil_mem"] = psutil.virtual_memory().percent
        extrastats["psutil_net"] = psutil.net_io_counters()
        extrastats["psutil_disk"] = psutil.disk_usage("/data")
        extrastats["seen_sd_hashes"] = len(index.seen_sd_hashes.cache)
        extrastats["disk_space_used"] = get_dir_size("/data")
        time.sleep(5)


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


def start_stats_thread():
    stats_thread = Thread(target=run_stats)
    stats_thread.daemon = True
    stats_thread.start()
