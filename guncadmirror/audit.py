from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, urlsplit

from .models import JobState, Release, ReleaseValidationError
from .paths import ensure_within
from .verification import hash_file

REPORT_SCHEMA = "guncad-mirror-archive-report-v1"
MANIFEST_SCHEMA = "guncad-mirror-publication-v1"


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    release_id: str
    sd_hash: str
    name: str
    channel_handle: str
    url: str | None
    url_lbry: str
    attempts: int
    claimed_size: int | None
    size: int | None
    claimed_sha384: str | None
    sha384: str | None
    sha256: str | None
    file_path: str | None
    torrent_path: str | None
    torrent_sha256: str | None
    btih: str | None
    magnet_uri: str | None
    acquisition_transport: str | None
    acquisition_source_url: str | None
    acquisition_lbry_failure: str | None
    updated_at: float
    valid: bool
    validation_errors: str


@dataclass(frozen=True, slots=True)
class FailureRecord:
    release_id: str
    sd_hash: str
    name: str
    channel_handle: str
    attempts: int
    next_attempt_at: float
    last_error: str
    updated_at: float


@dataclass(frozen=True, slots=True)
class AuditIssue:
    release_id: str | None
    sd_hash: str | None
    message: str


@dataclass(frozen=True, slots=True)
class ArchiveReport:
    generated_at: str
    data_dir: str
    rehashed_payloads: bool
    job_counts: Mapping[str, int]
    artifacts: tuple[ArtifactRecord, ...]
    failures: tuple[FailureRecord, ...]
    issues: tuple[AuditIssue, ...]
    orphan_manifests: tuple[str, ...]
    orphan_torrents: tuple[str, ...]

    def summary(self) -> dict[str, object]:
        valid = tuple(artifact for artifact in self.artifacts if artifact.valid)
        payloads = {
            artifact.sha384: artifact.size
            for artifact in valid
            if artifact.sha384 is not None and artifact.size is not None
        }
        transports = Counter(
            artifact.acquisition_transport or "unknown" for artifact in valid
        )
        return {
            "schema": REPORT_SCHEMA,
            "generated_at": self.generated_at,
            "data_dir": self.data_dir,
            "rehashed_payloads": self.rehashed_payloads,
            "job_counts": dict(sorted(self.job_counts.items())),
            "artifacts": {
                "total": len(self.artifacts),
                "valid": len(valid),
                "invalid": len(self.artifacts) - len(valid),
                "bytes": sum(artifact.size or 0 for artifact in valid),
                "unique_payloads": len(payloads),
                "unique_payload_bytes": sum(payloads.values()),
                "independently_claimed": sum(
                    artifact.claimed_sha384 is not None for artifact in valid
                ),
                "descriptor_only": sum(
                    artifact.claimed_sha384 is None for artifact in valid
                ),
                "acquisition_transports": dict(sorted(transports.items())),
            },
            "failures": len(self.failures),
            "integrity_issues": len(self.issues),
            "orphan_manifests": len(self.orphan_manifests),
            "orphan_torrents": len(self.orphan_torrents),
        }


def audit_archive(
    data_dir: Path,
    *,
    rehash_payloads: bool = False,
    progress: Callable[[int, int, Path], None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> ArchiveReport:
    data_dir = data_dir.resolve()
    state_path = data_dir / "mirror-state.sqlite3"
    if not state_path.is_file():
        raise FileNotFoundError(f"Mirror state database does not exist: {state_path}")

    uri = f"{state_path.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM jobs ORDER BY release_id, sd_hash"
        ).fetchall()

    job_counts = Counter(str(row["state"]) for row in rows)
    ready_rows = [row for row in rows if row["state"] == JobState.AWAITING_INDEX]
    artifacts: list[ArtifactRecord] = []
    failures: list[FailureRecord] = []
    issues: list[AuditIssue] = []
    expected_manifests: set[Path] = set()
    expected_torrents: set[Path] = set()

    for row in rows:
        if row["state"] == JobState.FAILED:
            failures.append(_failure_record(row))
        elif row["state"] != JobState.AWAITING_INDEX:
            issues.append(
                AuditIssue(
                    row["release_id"],
                    row["sd_hash"],
                    f"unfinished job state {row['state']!r}",
                )
            )

    total = len(ready_rows)
    for index, row in enumerate(ready_rows, 1):
        raw_file_path = row["file_path"]
        if progress is not None and isinstance(raw_file_path, str):
            progress(index, total, Path(raw_file_path))
        artifact, artifact_issues, manifest_path, torrent_path = _audit_artifact(
            data_dir,
            row,
            rehash_payloads=rehash_payloads,
        )
        artifacts.append(artifact)
        issues.extend(artifact_issues)
        expected_manifests.add(manifest_path)
        if torrent_path is not None:
            expected_torrents.add(torrent_path)

    outbox_dir = data_dir / "outbox"
    actual_manifests = (
        {path.resolve() for path in outbox_dir.glob("*/*/manifest.json")}
        if outbox_dir.is_dir()
        else set()
    )
    actual_torrents = (
        {path.resolve() for path in outbox_dir.glob("*/*/*.torrent")}
        if outbox_dir.is_dir()
        else set()
    )
    orphan_manifests = tuple(
        str(path) for path in sorted(actual_manifests - expected_manifests)
    )
    orphan_torrents = tuple(
        str(path) for path in sorted(actual_torrents - expected_torrents)
    )
    issues.extend(
        AuditIssue(None, None, f"orphan manifest: {path}") for path in orphan_manifests
    )
    issues.extend(
        AuditIssue(None, None, f"orphan torrent: {path}") for path in orphan_torrents
    )

    current_time = (now or (lambda: datetime.now(UTC)))()
    return ArchiveReport(
        generated_at=current_time.astimezone(UTC).isoformat(),
        data_dir=str(data_dir),
        rehashed_payloads=rehash_payloads,
        job_counts=dict(job_counts),
        artifacts=tuple(artifacts),
        failures=tuple(failures),
        issues=tuple(issues),
        orphan_manifests=orphan_manifests,
        orphan_torrents=orphan_torrents,
    )


def write_report(report: ArchiveReport, output_dir: Path) -> tuple[Path, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = (
        output_dir / "archive-summary.json",
        output_dir / "archive-artifacts.csv",
        output_dir / "archive-failures.csv",
        output_dir / "archive-issues.csv",
    )
    _atomic_write(
        paths[0],
        (json.dumps(report.summary(), indent=2, sort_keys=True) + "\n").encode(),
    )
    _write_csv(paths[1], report.artifacts, ArtifactRecord)
    _write_csv(paths[2], report.failures, FailureRecord)
    _write_csv(paths[3], report.issues, AuditIssue)
    return paths


def _audit_artifact(
    data_dir: Path,
    row: sqlite3.Row,
    *,
    rehash_payloads: bool,
) -> tuple[ArtifactRecord, list[AuditIssue], Path, Path | None]:
    release_id = str(row["release_id"])
    sd_hash = str(row["sd_hash"])
    errors: list[str] = []
    release = _release_from_row(row, errors)
    name = release.name if release is not None else ""
    channel = release.channel_handle if release is not None else ""
    if release is not None:
        _expect(
            errors, release.id == release_id, "stored release ID does not match job"
        )
        _expect(errors, release.sd_hash == sd_hash, "stored SD hash does not match job")
        if release.sha384 is not None:
            _expect(
                errors,
                row["sha384"] == release.sha384,
                "job SHA-384 does not match Index claim",
            )
    for field, length in (("sha384", 96), ("sha256", 64), ("info_hash", 40)):
        value = row[field]
        _expect(
            errors,
            isinstance(value, str)
            and len(value) == length
            and all(character in "0123456789abcdef" for character in value),
            f"job {field} is not a lowercase hexadecimal digest",
        )
    manifest_path = (
        data_dir / "outbox" / release_id / sd_hash / "manifest.json"
    ).resolve()

    file_path = _safe_path(data_dir, row["file_path"], "payload", errors)
    torrent_path = _safe_path(
        data_dir / "outbox", row["torrent_path"], "torrent", errors
    )
    document = _read_manifest(manifest_path, errors)
    actual_size: int | None = None
    torrent_sha256: str | None = None
    transport: str | None = None
    source_url: str | None = None
    lbry_failure: str | None = None

    if file_path is not None:
        if not file_path.is_file():
            errors.append(f"payload does not exist: {file_path}")
        else:
            actual_size = file_path.stat().st_size
            if actual_size == 0:
                errors.append("payload is empty")
            if release is not None and release.size is not None:
                _expect(
                    errors,
                    actual_size == release.size,
                    f"payload size {actual_size} != claimed {release.size}",
                )
            if rehash_payloads:
                hashes = hash_file(file_path)
                _expect(errors, hashes.sha384 == row["sha384"], "SHA-384 mismatch")
                _expect(errors, hashes.sha256 == row["sha256"], "SHA-256 mismatch")
                if release is not None and release.sha384 is not None:
                    _expect(
                        errors,
                        hashes.sha384 == release.sha384,
                        "SHA-384 does not match Index claim",
                    )

    if torrent_path is not None:
        if not torrent_path.is_file():
            errors.append(f"torrent does not exist: {torrent_path}")
        else:
            torrent_sha256 = _sha256_file(torrent_path)

    if document is not None:
        transport, source_url, lbry_failure = _validate_manifest(
            document,
            row,
            release,
            file_path,
            torrent_path,
            actual_size,
            torrent_sha256,
            errors,
        )

    issues = [AuditIssue(release_id, sd_hash, message) for message in errors]
    artifact = ArtifactRecord(
        release_id=release_id,
        sd_hash=sd_hash,
        name=name,
        channel_handle=channel,
        url=release.url if release is not None else None,
        url_lbry=release.url_lbry if release is not None else "",
        attempts=int(row["attempts"]),
        claimed_size=release.size if release is not None else None,
        size=actual_size,
        claimed_sha384=release.sha384 if release is not None else None,
        sha384=row["sha384"],
        sha256=row["sha256"],
        file_path=str(file_path) if file_path is not None else None,
        torrent_path=str(torrent_path) if torrent_path is not None else None,
        torrent_sha256=torrent_sha256,
        btih=row["info_hash"],
        magnet_uri=row["magnet_uri"],
        acquisition_transport=transport,
        acquisition_source_url=source_url,
        acquisition_lbry_failure=lbry_failure,
        updated_at=float(row["updated_at"]),
        valid=not errors,
        validation_errors=" | ".join(errors),
    )
    return artifact, issues, manifest_path, torrent_path


def _validate_manifest(
    document: Mapping[str, object],
    row: sqlite3.Row,
    release: Release | None,
    file_path: Path | None,
    torrent_path: Path | None,
    actual_size: int | None,
    torrent_sha256: str | None,
    errors: list[str],
) -> tuple[str | None, str | None, str | None]:
    _expect(
        errors, document.get("schema") == MANIFEST_SCHEMA, "manifest schema mismatch"
    )
    _expect(
        errors, document.get("status") == "awaiting-index", "manifest status mismatch"
    )
    release_doc = _mapping(document.get("release"), "release", errors)
    lbry_doc = _mapping(document.get("lbry"), "lbry", errors)
    artifact_doc = _mapping(document.get("artifact"), "artifact", errors)
    torrent_doc = _mapping(document.get("torrent"), "torrent", errors)
    acquisition_doc = document.get("acquisition")

    if release_doc is not None:
        _expect(
            errors, release_doc.get("id") == row["release_id"], "release ID mismatch"
        )
        if release is not None:
            _expect(
                errors, release_doc.get("name") == release.name, "release name mismatch"
            )
            _expect(
                errors,
                release_doc.get("channel_handle") == release.channel_handle,
                "channel handle mismatch",
            )
            _expect(
                errors, release_doc.get("url") == release.url, "release URL mismatch"
            )
            _expect(
                errors,
                release_doc.get("url_lbry") == release.url_lbry,
                "LBRY URL mismatch",
            )
    if lbry_doc is not None:
        _expect(errors, lbry_doc.get("sd_hash") == row["sd_hash"], "SD hash mismatch")
        if release is not None:
            _expect(
                errors,
                lbry_doc.get("claimed_sha384") == release.sha384,
                "claimed SHA-384 mismatch",
            )
    if artifact_doc is not None:
        _expect(
            errors, artifact_doc.get("size") == actual_size, "artifact size mismatch"
        )
        _expect(
            errors,
            artifact_doc.get("sha384") == row["sha384"],
            "manifest SHA-384 mismatch",
        )
        _expect(
            errors,
            artifact_doc.get("sha256") == row["sha256"],
            "manifest SHA-256 mismatch",
        )
        if file_path is not None:
            _expect(
                errors,
                artifact_doc.get("file_name") == file_path.name,
                "payload file name mismatch",
            )
    if torrent_doc is not None:
        _expect(errors, torrent_doc.get("btih") == row["info_hash"], "BTIH mismatch")
        _expect(
            errors,
            torrent_doc.get("magnet_uri") == row["magnet_uri"],
            "magnet URI mismatch",
        )
        _expect(
            errors,
            torrent_doc.get("sha256") == torrent_sha256,
            "torrent SHA-256 mismatch",
        )
        if torrent_path is not None:
            _expect(
                errors,
                torrent_doc.get("file_name") == torrent_path.name,
                "torrent file name mismatch",
            )
        piece_length = torrent_doc.get("piece_length")
        if (
            isinstance(piece_length, int)
            and piece_length > 0
            and actual_size is not None
        ):
            expected_pieces = (actual_size + piece_length - 1) // piece_length
            _expect(
                errors,
                torrent_doc.get("piece_count") == expected_pieces,
                "torrent piece count mismatch",
            )
        else:
            errors.append("torrent piece length is invalid")
        _validate_magnet(row["magnet_uri"], row["info_hash"], errors)

    return _acquisition_fields(acquisition_doc, release, errors)


def _acquisition_fields(
    value: object,
    release: Release | None,
    errors: list[str],
) -> tuple[str | None, str | None, str | None]:
    if value is None:
        return "lbry", None, None
    if not isinstance(value, Mapping):
        errors.append("manifest acquisition must be an object")
        return None, None, None
    transport = value.get("transport")
    source_url = value.get("source_url")
    lbry_failure = value.get("lbry_failure")
    if transport == "lbry":
        _expect(errors, source_url is None, "LBRY acquisition has a source URL")
        _expect(errors, lbry_failure is None, "LBRY acquisition has an LBRY failure")
        return "lbry", None, None
    if transport != "odysee-cdn":
        errors.append(f"unknown acquisition transport {transport!r}")
        return str(transport) if transport is not None else None, None, None
    if not isinstance(source_url, str):
        errors.append("Odysee acquisition has no source URL")
        source_url = None
    elif release is not None:
        parsed = urlsplit(source_url)
        parts = tuple(part for part in parsed.path.split("/") if part)
        _expect(
            errors,
            parsed.scheme == "https"
            and parsed.hostname == "player.odycdn.com"
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 443}
            and not parsed.fragment
            and release.id in parts
            and bool(parts)
            and parts[-1].startswith(f"{release.sd_hash[:6]}."),
            "Odysee source URL identity mismatch",
        )
    if not isinstance(lbry_failure, str) or not lbry_failure:
        errors.append("Odysee acquisition has no typed LBRY failure")
        lbry_failure = None
    return "odysee-cdn", source_url, lbry_failure


def _release_from_row(row: sqlite3.Row, errors: list[str]) -> Release | None:
    try:
        raw = json.loads(row["release_json"])
        return Release.from_api(raw)
    except (json.JSONDecodeError, ReleaseValidationError, TypeError) as error:
        errors.append(f"stored release metadata is invalid: {error}")
        return None


def _failure_record(row: sqlite3.Row) -> FailureRecord:
    errors: list[str] = []
    release = _release_from_row(row, errors)
    return FailureRecord(
        release_id=str(row["release_id"]),
        sd_hash=str(row["sd_hash"]),
        name=release.name if release is not None else "",
        channel_handle=release.channel_handle if release is not None else "",
        attempts=int(row["attempts"]),
        next_attempt_at=float(row["next_attempt_at"]),
        last_error=str(row["last_error"] or ""),
        updated_at=float(row["updated_at"]),
    )


def _safe_path(root: Path, value: object, label: str, errors: list[str]) -> Path | None:
    if not isinstance(value, str) or not value:
        errors.append(f"job has no {label} path")
        return None
    try:
        return ensure_within(root, Path(value))
    except ValueError as error:
        errors.append(str(error))
        return None


def _read_manifest(path: Path, errors: list[str]) -> Mapping[str, object] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"cannot read manifest {path}: {error}")
        return None
    if not isinstance(document, Mapping):
        errors.append("manifest must be a JSON object")
        return None
    return document


def _mapping(
    value: object, label: str, errors: list[str]
) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        errors.append(f"manifest {label} must be an object")
        return None
    return value


def _validate_magnet(magnet_uri: object, info_hash: object, errors: list[str]) -> None:
    if not isinstance(magnet_uri, str) or not isinstance(info_hash, str):
        errors.append("job has no magnet URI or BTIH")
        return
    parsed = urlsplit(magnet_uri)
    query = parse_qs(parsed.query)
    _expect(
        errors,
        parsed.scheme == "magnet" and query.get("xt") == [f"urn:btih:{info_hash}"],
        "magnet URI does not identify the job BTIH",
    )


def _expect(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def _write_csv(
    path: Path, records: Sequence[object], record_type: type[object]
) -> None:
    output = io.StringIO(newline="")
    field_names = tuple(field.name for field in fields(record_type))
    writer = csv.DictWriter(output, fieldnames=field_names, lineterminator="\n")
    writer.writeheader()
    if records:
        writer.writerows(asdict(record) for record in records)
    _atomic_write(path, output.getvalue().encode())


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
    temporary_path.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory and verify a GunCAD Mirror archive"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--rehash",
        action="store_true",
        help="read every payload and recompute SHA-384 and SHA-256",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = args.output_dir or args.data_dir / "reports"

    def progress(index: int, total: int, path: Path) -> None:
        if args.rehash:
            print(f"Rehashing {index}/{total}: {path}", file=sys.stderr, flush=True)

    try:
        report = audit_archive(
            args.data_dir,
            rehash_payloads=args.rehash,
            progress=progress,
        )
        paths = write_report(report, output_dir)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"Archive audit failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report.summary(), indent=2, sort_keys=True))
    print("Reports:", *(str(path) for path in paths), sep="\n  ")
    return 1 if report.issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
