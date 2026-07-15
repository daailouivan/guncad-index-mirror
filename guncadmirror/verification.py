from __future__ import annotations

import hashlib
from pathlib import Path

from .models import FileHashes, Release


class VerificationError(ValueError):
    """The reconstructed plaintext does not match its source metadata."""


def hash_file(path: Path, *, chunk_size: int = 1024**2) -> FileHashes:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    sha384 = hashlib.sha384()
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as payload:
        while chunk := payload.read(chunk_size):
            sha384.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
    return FileHashes(size=size, sha384=sha384.hexdigest(), sha256=sha256.hexdigest())


def verify_file(release: Release, path: Path) -> FileHashes:
    hashes = hash_file(path)
    if release.size is not None and hashes.size != release.size:
        raise VerificationError(
            f"size mismatch for {release.id}: got {hashes.size}, expected {release.size}"
        )
    if release.sha384 is not None and hashes.sha384 != release.sha384:
        raise VerificationError(
            f"SHA-384 mismatch for {release.id}: got {hashes.sha384}, expected {release.sha384}"
        )
    return hashes
