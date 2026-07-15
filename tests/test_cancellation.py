from __future__ import annotations

import unittest
from threading import Event

from guncadmirror.cancellation import (
    AcquisitionCancelled,
    check_cancelled,
    wait_or_cancel,
)


class CancellationTests(unittest.TestCase):
    def test_checks_optional_and_unset_events(self) -> None:
        check_cancelled(None)
        check_cancelled(Event())

    def test_set_event_cancels_checks_and_waits(self) -> None:
        stop = Event()
        stop.set()
        with self.assertRaises(AcquisitionCancelled):
            check_cancelled(stop)
        with self.assertRaises(AcquisitionCancelled):
            wait_or_cancel(stop, 10)

    def test_wait_uses_injected_sleep_without_an_event(self) -> None:
        delays: list[float] = []
        wait_or_cancel(None, 2.5, sleep=delays.append)
        self.assertEqual(delays, [2.5])

    def test_unset_event_can_finish_a_zero_length_wait(self) -> None:
        wait_or_cancel(Event(), 0)


if __name__ == "__main__":
    unittest.main()
