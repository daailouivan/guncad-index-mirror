import argparse
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta

from . import index, settings, stats, webui


def str_to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return value.strip().lower() in ("1", "true", "t", "yes", "on", "enabled")


def main():
    """
    Application entrypoint
    """
    stats.log("Started GunCAD Mirror")
    sleephours = 4

    # Slow down the urllib3 logger so it doesn't annoy users at startup
    urllib3_logger = logging.getLogger("urllib3.connectionpool")
    urllib3_logger.setLevel(logging.ERROR)

    # Set up our logger
    logger = logging.getLogger("guncad-mirror")
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s:%(lineno)d: %(message)s",
        level=logging.INFO,
    )

    # Set up the arg parser
    parser = argparse.ArgumentParser(
        prog="python -m guncadmirror",
        description="Mirror content from a GunCAD Index instance over LBRY",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging"
    )
    args = parser.parse_args()

    # Now that we have the logger, dump some quick info
    logger.info(f"Starting GunCAD Mirror {os.getenv('GUNCAD_COMMIT_REF', 'Unknown')}")

    # Parse out envvars as configs
    settings.parse_environment()

    # Set up some extra statistics
    stats.start_stats_thread()

    # If we have to start the webui thread, do so
    if settings.enable_webui:
        webui.start()

    # We've finished bootstrapping, wait for LBRY to do its thing
    logger.info("Started GunCAD Mirror")
    logger.info("Waiting for LBRY to start its wallet...")
    stats.extrastats["mirror_state"] = "Waiting for LBRY to start up"
    index.wait_for_component("wallet")
    stats.log("Finished waiting for LBRY to initialize", stdout=True)

    while True:
        logger.info(f"Cleaning sd_hash cache...")
        stats.extrastats["mirror_state"] = "Cleaning the sd_hash cache"
        index.seen_sd_hashes.cleanup()
        logger.info("Acquiring releases...")
        stats.extrastats["mirror_state"] = "Acquiring releases"
        starttime = time.perf_counter()
        try:
            for i, release in enumerate(index.get_releases(url=settings.endpoint)):
                try:
                    logger.info(f"Mirroring #{i + 1}: {release.get('name')}")
                    stats.extrastats["mirror_state"] = (
                        f"Mirroring #{i + 1}: {release.get('name')}"
                    )
                    changed = index.mirror(release, store_file=settings.assemble_files)
                    if changed:
                        stats.log(
                            f"+ Fetched new files for release #{i + 1}: {release.get('url')} \"{release.get('name')}\""
                        )
                except Exception as e:
                    logger.exception(e)
            subprocess.run(["hugo", "--source", "/app/guncad_mirror_hugo"])
        except Exception as e:
            logger.exception(e)
        sleepuntil = (datetime.now() + timedelta(hours=sleephours)).strftime("%I:%M %p")
        elapsed_time = time.perf_counter() - starttime
        stats.log(
            f"Completed in {webui.humanize_seconds(elapsed_time)}, sleeping for {sleephours}h (until {sleepuntil})",
            stdout=True,
        )
        stats.extrastats["mirror_state"] = f"Sleeping until {sleepuntil}"
        time.sleep(60 * 60 * sleephours)


if __name__ == "__main__":
    main()
