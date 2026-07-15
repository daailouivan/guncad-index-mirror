from __future__ import annotations

import copy
import json
import unittest

from guncadmirror.models import (
    Release,
    ReleaseValidationError,
    UnsupportedOriginError,
)

from .helpers import release_payload


class ReleaseTests(unittest.TestCase):
    def test_valid_v2_lbry_release_is_typed_and_serialized_stably(self) -> None:
        payload = release_payload()

        release = Release.from_api(payload)

        self.assertEqual(release.id, "a" * 40)
        self.assertEqual(release.channel_handle, "@channel:c")
        self.assertEqual(release.url, "https://odysee.com/release:r")
        self.assertEqual(release.url_lbry, "lbry://release#r")
        self.assertEqual(release.sd_hash, "b" * 96)
        self.assertEqual(release.size, len(b"payload"))
        self.assertEqual(json.loads(release.to_json()), payload)

    def test_rejects_non_lbry_origin_before_applying_lbry_schema(self) -> None:
        payload = release_payload()
        payload["id"] = "printables-1301807"
        payload["origin"] = {
            "platform": "printables",
            "external_id": "1301807",
            "links": [
                {
                    "name": "Printables",
                    "url": "https://printables.com/model/1301807",
                }
            ],
        }

        with self.assertRaisesRegex(
            UnsupportedOriginError, "unsupported release origin: printables"
        ):
            Release.from_api(payload)

    def test_accepts_lbry_only_release_without_an_odysee_link(self) -> None:
        payload = release_payload()
        payload["origin"]["links"] = [  # type: ignore[index]
            {"name": "Cannot be viewed on Odysee"},
            {"name": "LBRY Desktop", "url": "lbry://release#r"},
        ]

        release = Release.from_api(payload)

        self.assertIsNone(release.url)
        self.assertEqual(release.url_lbry, "lbry://release#r")

    def test_rejects_malformed_v2_lbry_releases(self) -> None:
        valid = release_payload()
        cases: list[tuple[object, str]] = []

        def changed(path: tuple[str, ...], value: object) -> dict[str, object]:
            payload = copy.deepcopy(valid)
            target = payload
            for key in path[:-1]:
                target = target[key]  # type: ignore[assignment,index]
            target[path[-1]] = value  # type: ignore[index]
            return payload

        cases.extend(
            [
                ([], "release must be a JSON object"),
                (changed(("origin",), None), "release origin"),
                (changed(("origin", "platform"), ""), "platform"),
                (changed(("id",), "bad"), "release id"),
                (changed(("channel",), None), "release channel"),
                (changed(("channel", "handle"), ""), "handle"),
                (changed(("origin", "external_id"), "c" * 40), "external_id"),
                (changed(("origin", "extra"), None), "origin extra"),
                (changed(("origin", "extra", "sd_hash"), "BAD"), "sd_hash"),
                (changed(("origin", "checksum"), "BAD"), "origin checksum"),
                (changed(("origin", "size"), True), "origin size"),
                (changed(("origin", "size"), 0), "origin size"),
                (changed(("origin", "links"), None), "origin links"),
                (changed(("origin", "links"), []), "LBRY"),
                (
                    changed(
                        ("origin", "links"),
                        [{"url": "https://user:pass@odysee.example/release"}],
                    ),
                    "LBRY",
                ),
                (
                    changed(
                        ("origin", "links"),
                        [{"url": "https://odysee.example/release"}],
                    ),
                    "LBRY",
                ),
                (changed(("name",), ""), "name"),
            ]
        )

        for payload, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ReleaseValidationError, message),
            ):
                Release.from_api(payload)  # type: ignore[arg-type]
