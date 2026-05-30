"""Single-instance lockfile for the orchestrator.

The lockfile holds the PID of the running orchestrator. Acquire is conservative:
a stale lockfile (PID gone, or PID recycled to a non-orchestrator process) is
treated as releasable. A live orchestrator PID blocks acquisition.
"""

from __future__ import annotations

import os
from pathlib import Path


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
