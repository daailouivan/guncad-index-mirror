from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from guncadmirror.models import Release


def release_payload(
    content: bytes = b"payload",
    *,
    release_id: str = "a" * 40,
    sd_hash: str = "b" * 96,
    channel: str = "@channel:c",
    name: str = "Release Name",
) -> dict[str, Any]:
    return {
        "id": release_id,
        "name": name,
        "path": f"/{channel}/release:r",
        "channel": {"handle": channel},
        "origin": {
            "platform": "lbry",
            "slug": "release:r",
            "external_id": release_id,
            "size": len(content),
            "popularity": 1.0,
            "checksum": hashlib.sha384(content).hexdigest(),
            "links": [
                {"name": "Odysee", "url": "https://odysee.com/release:r"},
                {"name": "LBRY Desktop", "url": "lbry://release%23r"},
            ],
            "extra": {"sd_hash": sd_hash, "lbry_only": False},
        },
    }


def make_release(content: bytes = b"payload", **overrides: Any) -> Release:
    return Release.from_api(release_payload(content, **overrides))


class FakeResponse:
    def __init__(
        self,
        payload: Any = None,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        status_error: Exception | None = None,
        json_error: Exception | None = None,
    ):
        self.payload = payload
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.status_error = status_error
        self.json_error = json_error

    def raise_for_status(self) -> None:
        if self.status_error:
            raise self.status_error

    def json(self) -> Any:
        if self.json_error:
            raise self.json_error
        return self.payload


class QueueSession:
    def __init__(self, *responses: Any):
        self.responses = deque(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("get", url, kwargs))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("post", url, kwargs))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path
