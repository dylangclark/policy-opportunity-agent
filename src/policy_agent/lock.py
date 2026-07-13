from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class AlreadyRunningError(RuntimeError):
    pass


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Prevent overlapping systemd/manual runs on Linux, including Raspberry Pi OS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunningError(f"Another policy-agent run holds {path}") from exc
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(__import__("os").getpid()))
            handle.flush()
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
