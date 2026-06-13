"""Single-instance lockfile for the orchestrator.

The lockfile holds the PID of the running orchestrator. Acquire is conservative:
a stale lockfile (PID gone, or PID recycled to a non-orchestrator process) is
treated as releasable. A live orchestrator PID blocks acquisition.
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def file_lock(path: str | Path, *, timeout: float = 30.0, poll: float = 0.1):
    """Cross-process advisory exclusive lock via ``fcntl.flock``.

    Blocks until the lock is acquired or ``timeout`` seconds elapse (then raises
    ``TimeoutError``). The lock is tied to the open file description, so it
    auto-releases on context exit *and* on process death — a crashed holder
    never leaves a stale lock (unlike the PID-based orchestrator lock above).

    Used to serialize read-modify-write on shared mutable files (the crontab and
    ``registry.json``) now that fork branches run multiple orchestrators at once.

    NOT re-entrant: a second ``file_lock`` on the same path *in the same process*
    opens a distinct fd and will block (the flock is per open-file-description).
    Structure callers so one lock acquisition spans the whole read-modify-write.

    Lock ordering: when more than one ``file_lock`` is held at once, always
    acquire the registry lock BEFORE the crontab lock to avoid deadlock.

    ``fcntl.flock`` is reliable on local filesystems but NOT over NFS; the agent
    root is local on this host.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire lock {p} within {timeout}s"
                    )
                time.sleep(poll)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_pid(path: str | Path) -> int | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission to signal it.
        return True
    except OSError:
        return False
    return True


def _pid_is_orchestrator(pid: int) -> bool:
    """Best-effort: confirm /proc/<pid>/cmdline mentions orchestrator.py."""
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if not cmdline_path.exists():
        return False
    try:
        raw = cmdline_path.read_bytes()
    except OSError:
        return False
    # cmdline is NUL-separated argv
    cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    return "orchestrator.py" in cmd


def acquire(path: str | Path) -> bool:
    """Try to acquire the lockfile. Returns True on success."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing_pid = read_pid(p)
    if existing_pid is not None:
        if is_pid_alive(existing_pid) and _pid_is_orchestrator(existing_pid):
            return False
        # Stale or recycled PID — overwrite.
    try:
        p.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return False
    return True


def release(path: str | Path) -> None:
    p = Path(path)
    try:
        # Only remove if it's still ours, to avoid clobbering a successor.
        pid = read_pid(p)
        if pid is None or pid == os.getpid():
            p.unlink(missing_ok=True)
    except OSError:
        pass
