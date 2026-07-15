from __future__ import annotations

import json
import unittest

from guncadmirror.models import Release, ReleaseValidationError

from .helpers import release_payload


class ReleaseTests(unittest.TestCase):
    def test_valid_release_is_typed_and_serialized_stably(self):
        payload = release_payload()
        release = Release.from_api(payload)

        self.assertEqual(release.id, "a" * 40)
        self.assertEqual(release.channel_handle, "@channel:c")
        self.assertEqual(json.loads(release.to_json()), payload)

    def test_optional_checksum_and_size(self):
        payload = release_payload()
        payload["sha384sum"] = ""
        payload["size"] = None

        release = Release.from_api(payload)

        self.assertIsNone(release.sha384)
        self.assertIsNone(release.size)

    def test_rejects_malformed_releases(self):
        valid = release_payload()
        cases = [
            ([], "release must be a JSON object"),
            ({**valid, "id": "bad"}, "release id"),
            ({**valid, "channel": None}, "release channel"),
            ({**valid, "sd_hash": "BAD"}, "sd_hash"),
            ({**valid, "sha384sum": "BAD"}, "sha384sum"),
            ({**valid, "size": True}, "release size"),
            ({**valid, "size": 0}, "release size"),
            ({**valid, "name": ""}, "name"),
            ({**valid, "url": None}, "url"),
            ({**valid, "channel": {"handle": ""}}, "handle"),
        ]

        for payload, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ReleaseValidationError, message),
            ):
                Release.from_api(payload)
