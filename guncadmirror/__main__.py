import argparse
import logging
import os
import time
from datetime import datetime, timedelta

from . import index, stats, webui


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

    # Parse out envvars as configs
    assemble_files = os.getenv("MIRROR_ASSEMBLE_FILES", False)
    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)
    if assemble_files:
        logger.info(
            "MIRROR_ASSEMBLE_FILES is set -- we will mirror WHOLE FILES. Note that this uses TWICE AS MUCH DISK as not doing so."
        )
    enable_webui = os.getenv("MIRROR_ENABLE_WEBUI", False)
    if enable_webui:
        logger.info(
            "MIRROR_ENABLE_WEBUI is set -- view stats on :8081 (or whatever port you forwarded that to)"
        )
        webui.start()

    # Set up some extra statistics
    stats.start_stats_thread()
    stats.extrastats["mirror_api_endpoint"] = os.getenv(
        "MIRROR_API_ENDPOINT",
        "https://guncadindex.com/api/releases/?format=json&limit=100",
    )
    stats.extrastats["mirror_assemble_files"] = assemble_files
    stats.extrastats["mirror_enable_webui"] = enable_webui

    # We've finished bootstrapping, wait for LBRY to do its thing
    logger.info("Started GunCAD Mirror")
    logger.info("Waiting for LBRY to start its wallet...")
    stats.extrastats["mirror_state"] = "Waiting for LBRY to start up"
    index.wait_for_component("wallet")
    stats.log("Finished waiting for LBRY to initialize")

    while True:
        logger.info(f"Cleaning sd_hash cache...")
        stats.extrastats["mirror_state"] = "Cleaning the sd_hash cache"
        index.seen_sd_hashes.cleanup()
        logger.info("Acquiring releases...")
        stats.extrastats["mirror_state"] = "Acquiring releases"
        try:
            for i, release in enumerate(
                index.get_releases(
                    url=os.getenv(
                        "MIRROR_API_ENDPOINT",
                        "https://guncadindex.com/api/releases/?format=json&limit=25",
                    )
                )
            ):
                try:
                    logger.info(f"Mirroring #{i + 1}:  {release.get('name')}")
                    stats.extrastats["mirror_state"] = (
                        f"Mirroring #{i + 1}: {release.get('name')}"
                    )
                    changed = index.mirror(release, store_file=assemble_files)
                    if changed:
                        stats.log(
                            f"+ Fetched new files for release #{i + 1}: {release.get('url')} \"{release.get('name')}\""
                        )
                except Exception as e:
                    logger.exception(e)
        except Exception as e:
            logger.exception(e)
        sleepuntil = (datetime.now() + timedelta(hours=sleephours)).strftime("%I:%M %p")
        logger.info(f"Sleeping for {sleephours}h (until {sleepuntil})")
        stats.extrastats["mirror_state"] = f"Sleeping until {sleepuntil}"
        time.sleep(60 * 60 * sleephours)


if __name__ == "__main__":
    main()
