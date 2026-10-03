from __future__ import annotations

import copy
import hashlib
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
        self.assertEqual(release.popularity, 1.0)
        self.assertFalse(release.lbry_only)
        self.assertEqual(json.loads(release.to_json()), payload)

    def test_rejects_unsupported_origin_before_applying_schema(self) -> None:
        payload = release_payload()
        payload["origin"]["platform"] = "unsupported_platform"

        with self.assertRaisesRegex(
            UnsupportedOriginError, "unsupported release origin: unsupported_platform"
        ):
            Release.from_api(payload)

    def test_accepts_printables_release_with_synthetic_sd_hash(self) -> None:
        payload = {
            "id": "printables-1863745",
            "name": "AmmoBox 22LR",
            "channel": {"handle": "MarcinMJessa_5279187"},
            "origin": {
                "platform": "printables",
                "external_id": "1863745",
                "checksum": "7787145",
                "size": 757847,
                "popularity": 1.25,
                "links": [
                    {
                        "name": "Printables",
                        "url": "https://printables.com/model/1863745-ammobox-22lr",
                    }
                ],
            },
        }

        release = Release.from_api(payload)

        self.assertEqual(release.id, "printables-1863745")
        self.assertEqual(release.platform, "printables")
        self.assertEqual(release.external_id, "1863745")
        self.assertEqual(release.channel_handle, "MarcinMJessa_5279187")
        self.assertEqual(release.url, "https://printables.com/model/1863745-ammobox-22lr")
        self.assertIsNone(release.url_lbry)
        self.assertIsNone(release.sha384)
        self.assertEqual(release.size, 757847)
        self.assertFalse(release.lbry_only)
        # Verify synthetic sd_hash is a valid 96-char lowercase SHA-384
        self.assertEqual(len(release.sd_hash), 96)
        expected_sd_hash = hashlib.sha384(b"printables:printables-1863745").hexdigest()
        self.assertEqual(release.sd_hash, expected_sd_hash)

    def test_accepts_github_release_with_synthetic_sd_hash(self) -> None:
        payload = {
            "id": "github-org:repo-v1.0",
            "name": "Repo Release 1.0",
            "channel": {"handle": "org"},
            "origin": {
                "platform": "github",
                "external_id": "org/repo",
                "size": 1024,
                "popularity": 2.0,
                "links": [
                    {
                        "name": "GitHub",
                        "url": "https://github.com/org/repo/releases/tag/v1.0",
                    }
                ],
            },
        }

        release = Release.from_api(payload)

        self.assertEqual(release.id, "github-org:repo-v1.0")
        self.assertEqual(release.platform, "github")
        self.assertEqual(release.channel_handle, "org")
        self.assertIsNone(release.url_lbry)
        self.assertEqual(
            release.sd_hash,
            hashlib.sha384(b"github:github-org:repo-v1.0").hexdigest(),
        )

    def test_accepts_lbry_only_release_without_an_odysee_link(self) -> None:
        payload = release_payload()
        payload["origin"]["extra"]["lbry_only"] = True  # type: ignore[index]
        payload["origin"]["links"] = [  # type: ignore[index]
            {"name": "Cannot be viewed on Odysee"},
            {"name": "LBRY Desktop", "url": "lbry://release#r"},
        ]

        release = Release.from_api(payload)

        self.assertIsNone(release.url)
        self.assertEqual(release.url_lbry, "lbry://release#r")
        self.assertTrue(release.lbry_only)

    def test_accepts_legacy_release_without_size_or_checksum(self) -> None:
        payload = release_payload()
        payload["origin"]["size"] = 0  # type: ignore[index]
        payload["origin"]["checksum"] = None  # type: ignore[index]

        release = Release.from_api(payload)

        self.assertIsNone(release.size)
        self.assertIsNone(release.sha384)

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
                (
                    changed(("origin", "extra", "lbry_only"), "yes"),
                    "lbry_only",
                ),
                (changed(("origin", "checksum"), "BAD"), "origin checksum"),
                (changed(("origin", "size"), True), "origin size"),
                (changed(("origin", "size"), -1), "origin size"),
                (changed(("origin", "popularity"), True), "origin popularity"),
                (changed(("origin", "popularity"), -1), "origin popularity"),
                (changed(("origin", "popularity"), "popular"), "origin popularity"),
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
