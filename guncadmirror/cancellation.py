from __future__ import annotations

import time
from collections.abc import Callable
from threading import Event


class AcquisitionCancelled(RuntimeError):
    """An operator requested a resumable stop during acquisition."""


def check_cancelled(stop: Event | None) -> None:
    if stop is not None and stop.is_set():
        raise AcquisitionCancelled("operator requested a resumable stop")


def wait_or_cancel(
    stop: Event | None,
    delay: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if stop is None:
        sleep(delay)
    elif stop.wait(delay):
        raise AcquisitionCancelled("operator requested a resumable stop")
