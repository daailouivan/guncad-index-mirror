from __future__ import annotations

import argparse
import logging
import signal
from dataclasses import replace
from threading import Event
from typing import Sequence

from .cancellation import AcquisitionCancelled
from .runtime import build_runtime
from .settings import ConfigurationError, Settings


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m guncadmirror",
        description="Evacuate LBRY releases into verified BitTorrent artifacts",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )
    parser.add_argument(
        "--once", action="store_true", help="run one Index cycle and exit"
    )
    parser.add_argument(
        "--max-releases",
        type=int,
        help="override MIRROR_MAX_RELEASES_PER_RUN for this process",
    )
    args = parser.parse_args(argv)
    if args.max_releases is not None and args.max_releases < 1:
        parser.error("--max-releases must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s:%(lineno)d: %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )
    logger = logging.getLogger("guncad-mirror")

    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        logger.error("Invalid configuration: %s", error)
        return 2
    if args.max_releases is not None:
        settings = replace(settings, max_releases_per_run=args.max_releases)

    stop = Event()
    runtime = None

    def request_stop(signum: int, _frame: object) -> None:
        logger.info(
            "Received signal %d; preserving resumable state and stopping", signum
        )
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        runtime = build_runtime(settings)
        runtime.start(stop)
        if args.once:
            result = runtime.run_cycle(stop)
            return 1 if result.failed else 0
        runtime.run_forever(stop)
        return 0
    except AcquisitionCancelled:
        logger.info("Stop requested; resumable state is intact")
        return 0
    except KeyboardInterrupt:
        logger.info("Interrupted")
        return 130
    except Exception:
        logger.exception("GunCAD Mirror stopped after a fatal error")
        return 1
    finally:
        if runtime is not None:
            runtime.stop()


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
