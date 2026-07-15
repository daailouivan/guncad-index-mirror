from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any

from guncadmirror.sessions import ThreadLocalSessionPool


class FakeSession:
    def __init__(self, identity: int) -> None:
        self.identity = identity
        self.closed = False

    def get(self, _url: str, **_kwargs: Any) -> int:
        return self.identity

    def post(self, _url: str, **_kwargs: Any) -> int:
        return self.identity

    def close(self) -> None:
        self.closed = True


class ThreadLocalSessionPoolTests(unittest.TestCase):
    def test_reuses_one_session_per_thread_and_closes_every_session(self) -> None:
        sessions: list[FakeSession] = []
        lock = Lock()

        def factory() -> FakeSession:
            with lock:
                session = FakeSession(len(sessions))
                sessions.append(session)
            return session

        pool = ThreadLocalSessionPool(factory)  # type: ignore[arg-type]

        def use_session(_value: int) -> tuple[int, int]:
            return pool.get("https://example.test"), pool.post("https://example.test")

        with ThreadPoolExecutor(max_workers=3) as executor:
            results = tuple(executor.map(use_session, range(12)))

        self.assertTrue(all(first == second for first, second in results))
        self.assertGreaterEqual(len(sessions), 1)
        self.assertLessEqual(len(sessions), 3)
        pool.close()
        pool.close()
        self.assertTrue(all(session.closed for session in sessions))
        with self.assertRaisesRegex(RuntimeError, "closed"):
            pool.get("https://example.test")


if __name__ == "__main__":
    unittest.main()
