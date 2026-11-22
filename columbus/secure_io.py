"""Atomic private file I/O, locking, dynamic secret loading, and redaction."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

MAX_JSON_BYTES = 2 * 1024 * 1024  # 2 MiB safety cap


class SecureIOError(Exception):
    """Raised on private state or secret I/O failure."""


def make_private_dir(path: Path) -> Path:
    """Create directory with 0700 permissions."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically write bytes with restricted permissions and fsync."""
    make_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.chmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_json(path: Path, payload: Any, mode: int = 0o600) -> None:
    """Atomically serialize JSON with allow_nan=False and 0600 permissions."""
    raw = (json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    atomic_write_bytes(path, raw, mode=mode)


def read_json(path: Path, max_bytes: int = MAX_JSON_BYTES) -> Any:
    """Read and parse bounded JSON from a file without buffering unbounded data."""
    with path.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SecureIOError("json_size_limit")
    return json.loads(data.decode("utf-8"))


def load_json_stream(stream: Any, max_bytes: int = MAX_JSON_BYTES) -> Any:
    """Read bounded JSON from a binary stream (such as sys.stdin.buffer)."""
    raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise SecureIOError("request_size_limit")
    if not raw.strip():
        raise ValueError("empty_json")
    return json.loads(raw.decode("utf-8"))


@contextlib.contextmanager
def file_lock(lock_path: Path, blocking: bool = False) -> Iterator[int]:
    """Acquire an advisory flock on lock_path."""
    make_private_dir(lock_path.parent)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            raise SecureIOError("lock_busy") from exc
        yield fd
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def load_api_key(key_file: Path) -> str:
    """Dynamically load a provider API key from a 0600 non-symlink file using fstat on the open FD."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(key_file), flags)
    except FileNotFoundError as exc:
        raise SecureIOError("api_key_missing") from exc
    except OSError as exc:
        raise SecureIOError("api_key_open_failed") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise SecureIOError("api_key_not_regular_file")
        if st.st_mode & 0o077:
            raise SecureIOError("api_key_insecure_permissions")
        if st.st_size > 4096:
            raise SecureIOError("api_key_size_limit")
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            raw = fh.read(4097).strip()
    finally:
        if fd >= 0:
            os.close(fd)
    if not raw:
        raise SecureIOError("api_key_empty")
    return raw


def redact_secrets(text: str, secrets: Iterable[Optional[str]]) -> str:
    """Replace any non-empty secret substrings with <REDACTED>."""
    out = text
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "<REDACTED>")
    return out


def pid_is_alive(pid: int) -> bool:
    """Return True if a POSIX PID currently exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
