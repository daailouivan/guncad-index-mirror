from __future__ import annotations

from collections.abc import Callable
from threading import Lock, local
from typing import Any

import requests


class ThreadLocalSessionPool:
    """Give each worker thread its own requests session and close them together."""

    def __init__(
        self,
        factory: Callable[[], requests.Session] = requests.Session,
    ) -> None:
        self.factory = factory
        self._local = local()
        self._lock = Lock()
        self._sessions: list[requests.Session] = []
        self._closed = False

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._session().get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._session().post(url, **kwargs)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions = tuple(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session
        with self._lock:
            if self._closed:
                raise RuntimeError("HTTP session pool is closed")
            session = self.factory()
            self._sessions.append(session)
            self._local.session = session
        return session
