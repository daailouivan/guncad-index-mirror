from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from guncadmirror.cancellation import AcquisitionCancelled
from guncadmirror.verification import VerificationError, hash_file, verify_file

from .helpers import make_release


class VerificationTests(unittest.TestCase):
    def test_hashes_and_verifies_streaming_payload(self) -> None:
        content = b"abcdefghijk"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "payload.bin"
            path.write_bytes(content)
            progress: list[int] = []
            hashes = hash_file(path, chunk_size=3, progress=progress.append)

        self.assertEqual(hashes.size, len(content))
        self.assertEqual(hashes.sha384, hashlib.sha384(content).hexdigest())
        self.assertEqual(hashes.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(progress, [0, 3, 6, 9, 11])

    def test_verification_checks_size_and_external_sha384(self) -> None:
        content = b"payload"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "payload.bin"
            path.write_bytes(content)
            release = make_release(content)
            self.assertEqual(verify_file(release, path).size, len(content))

            with self.assertRaisesRegex(VerificationError, "size mismatch"):
                verify_file(replace(release, size=999), path)
            with self.assertRaisesRegex(VerificationError, "SHA-384 mismatch"):
                verify_file(replace(release, sha384="0" * 96), path)

            uncorroborated = replace(release, size=None, sha384=None)
            self.assertEqual(verify_file(uncorroborated, path).size, len(content))

    def test_verification_allows_size_variance_for_non_lbry_platforms(self) -> None:
        content = b"packaged-archive-content"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "payload.bin"
            path.write_bytes(content)
            release = replace(make_release(content), platform="printables", size=999999)
            verified = verify_file(release, path)
            self.assertEqual(verified.size, len(content))

    def test_verification_rejects_empty_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "empty.bin"
            path.write_bytes(b"")
            release = make_release(b"something")
            with self.assertRaisesRegex(VerificationError, "empty"):
                verify_file(release, path)

    def test_rejects_nonpositive_chunk_size(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            hash_file(Path("unused"), chunk_size=0)

    def test_hashing_is_cancellable_between_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "payload.bin"
            path.write_bytes(b"two chunks")
            stop = Mock()
            stop.is_set.side_effect = [False, True]
            with self.assertRaises(AcquisitionCancelled):
                hash_file(path, chunk_size=3, stop=stop)


if __name__ == "__main__":
    unittest.main()
