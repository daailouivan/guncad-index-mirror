import collections
import logging
import os
import time
from datetime import datetime
from threading import Thread

import psutil

from . import index

extrastats = {}

extralog = collections.deque(maxlen=512)


def collect():
    extrastats["version"] = os.getenv("GUNCAD_COMMIT_REF", "Unknown")
    extrastats["psutil_cpu"] = psutil.cpu_percent(interval=0.2)
    extrastats["psutil_mem"] = psutil.virtual_memory().percent
    extrastats["psutil_net"] = psutil.net_io_counters()
    extrastats["psutil_disk"] = psutil.disk_usage("/data")
    extrastats["seen_sd_hashes"] = len(index.seen_sd_hashes.cache)


def collect_expensive(prime=False):
    extrastats["disk_space_used"] = 0 if prime else get_dir_size("/data")


def run_stats():
    logger = logging.getLogger("guncad-mirror")
    while True:
        try:
            collect()
        except Exception as e:
            logger.error("Exception in stats collection thread:")
            logger.error(e, exc_info=True)
        time.sleep(1)


def run_expensive_stats():
    logger = logging.getLogger("guncad-mirror")
    while True:
        try:
            collect_expensive()
        except Exception as e:
            logger.error("Exception in stats collection thread:")
            logger.error(e, exc_info=True)
        time.sleep(30)


def log(string, stdout=False):
    logger = logging.getLogger("guncad-mirror")
    if stdout:
        logger.info(string)
    return extralog.append(f"[{datetime.now()}] {string}")


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
    # Do one collection run before returning so the app doesn't use stale stats
    collect()
    collect_expensive(prime=True)
    stats_thread = Thread(target=run_stats)
    stats_thread.daemon = True
    stats_thread.start()
    log("Started cheap stats collector thread", stdout=True)
    stats_expensive_thread = Thread(target=run_expensive_stats)
    stats_expensive_thread.daemon = True
    stats_expensive_thread.start()
    log("Started expensive stats collector thread", stdout=True)
