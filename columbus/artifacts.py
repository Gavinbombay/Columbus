"""Bounded, 0600 artifact staging and SHA-256 verification."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Dict, List

from columbus.secure_io import atomic_write_bytes, make_private_dir

MAX_FILES = 4
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ArtifactError(Exception):
    """Raised when artifact validation or size limits fail."""


def validate_artifact_name(name: str) -> str:
    if not isinstance(name, str) or not SAFE_NAME_RE.match(name) or ".." in name:
        raise ArtifactError("invalid_artifact_name")
    return name


def write_artifacts(artifacts_dir: Path, files: Dict[str, bytes]) -> List[Dict[str, object]]:
    """Write bounded output artifacts with 0600 mode and SHA-256 receipts."""
    if len(files) > MAX_FILES:
        raise ArtifactError("artifact_file_count_limit")
    total = 0
    for name, data in files.items():
        validate_artifact_name(name)
        if not isinstance(data, (bytes, bytearray)):
            raise ArtifactError("artifact_not_bytes")
        size = len(data)
        if size > MAX_FILE_BYTES:
            raise ArtifactError("artifact_file_size_limit")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise ArtifactError("artifact_total_size_limit")

    make_private_dir(artifacts_dir)
    receipts: List[Dict[str, object]] = []
    for name, data in sorted(files.items()):
        dest = artifacts_dir / name
        raw = bytes(data)
        atomic_write_bytes(dest, raw, mode=0o600)
        receipts.append({
            "name": name,
            "path": str(dest),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    return receipts
