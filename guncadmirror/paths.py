from __future__ import annotations

import re
import unicodedata
from pathlib import Path

UNSAFE_COMPONENTS = {"", ".", ".."}
SEPARATOR_RE = re.compile(r"[^A-Za-z0-9._#@+-]+")


def safe_component(value: str, *, fallback: str, max_length: int = 96) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    component = SEPARATOR_RE.sub("-", ascii_value).strip(" .-")
    if component in UNSAFE_COMPONENTS:
        component = fallback
    return component[:max_length] or fallback


def release_directory(
    data_dir: Path, channel_handle: str, release_name: str, sd_hash: str
) -> Path:
    channel = safe_component(
        channel_handle.replace(":", "#"), fallback="unknown-channel"
    )
    name = safe_component(release_name, fallback="unnamed-release")
    return data_dir / channel / f"{name}-{sd_hash[:12]}"


def ensure_within(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if not resolved_candidate.is_relative_to(resolved_root):
        raise ValueError(f"path escapes configured data directory: {candidate}")
    return resolved_candidate
