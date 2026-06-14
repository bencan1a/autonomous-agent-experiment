"""Instance-aware crontab control.

Each instance's cron entry is addressed by a tagged comment on the line ABOVE
the entry::

    # agent-instance:<id>
    47 2 30 5 * cd /path/to/agent && ./venv/bin/python orchestrator.py --instance <id> >> instances/<id>/logs/orchestrator.log 2>&1

Strategy: the orchestrator removes its own instance's entry FIRST THING each
wake, then re-installs a one-shot entry at the agent's chosen future minute when
the session ends. ``instance_manager`` uses the same helpers for activate /
pause / resume. We only ever touch lines tagged for the given instance — never
untagged lines (e.g. the @reboot dashboard entry).
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from lockfile import file_lock

logger = logging.getLogger(__name__)

AGENT_ROOT = Path(__file__).resolve().parent
TAG_PREFIX = "# agent-instance:"
MIN_INTERVAL_MINUTES = 30

# Serializes every crontab read-modify-write. With fork branches running
# multiple orchestrators concurrently, two interleaved `crontab -l`/`crontab -`
# cycles would otherwise clobber each other's lines (a branch could silently
# lose its next-wake entry). Acquire AFTER the registry lock if holding both.
CRONTAB_LOCK = AGENT_ROOT / ".crontab.lock"


def instance_marker(instance_id: str) -> str:
    return f"{TAG_PREFIX}{instance_id}"


def cron_command(instance_id: str) -> str:
    """The command line a cron entry runs for an instance."""
    py = AGENT_ROOT / "venv" / "bin" / "python"
    log_path = AGENT_ROOT / "instances" / instance_id / "logs" / "orchestrator.log"
    return (
        f"cd {AGENT_ROOT} && {py} orchestrator.py --instance {instance_id} "
        f">> {log_path} 2>&1"
    )


# --------------------------------------------------------------------------- #
# crontab read / write
# --------------------------------------------------------------------------- #

def _read_crontab() -> list[str]:
    res = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if res.returncode != 0:
        return []  # "no crontab for user" -> empty
    return res.stdout.splitlines()


def _write_crontab(lines: list[str]) -> None:
    body = "\n".join(lines)
    if body and not body.endswith("\n"):
        body += "\n"
    res = subprocess.run(["crontab", "-"], input=body, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"crontab install failed: {res.stderr}")


# --------------------------------------------------------------------------- #
# instance-scoped operations
# --------------------------------------------------------------------------- #

def _filter_instance_lines(lines: list[str], instance_id: str) -> tuple[list[str], int]:
    """Pure helper: drop the tagged comment + its following entry line.

    Returns (kept_lines, removed_count). Untagged lines are never touched. Takes
    no lock — callers hold ``CRONTAB_LOCK`` around the surrounding read+write.
    """
    marker = instance_marker(instance_id)
    kept: list[str] = []
    skip_next = False
    removed = 0
    for ln in lines:
        if skip_next:
            # This is the cron entry directly under the marker we just dropped.
            skip_next = False
            removed += 1
            continue
        if ln.strip() == marker:
            removed += 1
            skip_next = True
            continue
        kept.append(ln)
    return kept, removed


def remove_instance_entries(instance_id: str) -> int:
    """Remove the tagged comment line and its following entry line, if present.

    Returns the number of lines removed. Untagged lines are never touched.
    """
    with file_lock(CRONTAB_LOCK):
        kept, removed = _filter_instance_lines(_read_crontab(), instance_id)
        if removed:
            _write_crontab(kept)
            logger.info("Removed %d cron line(s) for instance %s", removed, instance_id)
    return removed


def install_instance_one_shot(
    instance_id: str, *, minutes_from_now: int,
    min_minutes: int = MIN_INTERVAL_MINUTES, command: str | None = None,
) -> str:
    """Install a single (marker, entry) pair firing once at now+minutes.

    This is the SINGLE authority on the wake-interval floor: ``minutes_from_now``
    is clamped to ``min_minutes`` (default ``MIN_INTERVAL_MINUTES``). Callers that
    expose a per-instance ``min_interval_minutes`` knob pass it here rather than
    pre-clamping, so the config value is honored exactly (e.g. an operator-set
    floor of 10 is respected instead of being silently bumped back to 30)."""
    if minutes_from_now < min_minutes:
        logger.warning(
            "Requested interval %d below minimum %d; clamping",
            minutes_from_now, min_minutes,
        )
        minutes_from_now = min_minutes

    cmd = command or cron_command(instance_id)
    # cron uses local server time; use naive 'now' to match.
    target = datetime.now() + timedelta(minutes=minutes_from_now)
    expr = f"{target.minute} {target.hour} {target.day} {target.month} *"

    # Remove-then-append must happen under ONE lock acquisition (file_lock is not
    # re-entrant) so a concurrent orchestrator can't interleave between them.
    with file_lock(CRONTAB_LOCK):
        kept, _ = _filter_instance_lines(_read_crontab(), instance_id)
        kept.append(instance_marker(instance_id))
        kept.append(f"{expr} {cmd}")
        _write_crontab(kept)
    logger.info("Installed cron for %s: fires %s", instance_id, target.isoformat())
    return f"{expr} {cmd}"


def clear_instance(instance_id: str) -> None:
    remove_instance_entries(instance_id)


def current_instance_entries(instance_id: str) -> list[str]:
    """Return the cron entry line(s) (not the marker) installed for an instance."""
    marker = instance_marker(instance_id)
    lines = _read_crontab()
    out: list[str] = []
    take_next = False
    for ln in lines:
        if take_next:
            out.append(ln)
            take_next = False
            continue
        if ln.strip() == marker:
            take_next = True
    return out


def next_fire_at(instance_id: str) -> datetime | None:
    """Parse the instance's cron entry and return its next-fire datetime (local naive)."""
    entries = current_instance_entries(instance_id)
    if not entries:
        return None
    fields = entries[0].split()
    if len(fields) < 5:
        return None
    try:
        minute, hour, day, month = (int(fields[i]) for i in range(4))
    except ValueError:
        return None
    now = datetime.now()
    for year in (now.year, now.year + 1):
        try:
            target = datetime(year, month, day, hour, minute)
        except ValueError:
            return None
        if target >= now:
            return target
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        iid = sys.argv[1]
        print(f"Cron entries for instance '{iid}':")
        for ln in current_instance_entries(iid):
            print(" ", ln)
        nf = next_fire_at(iid)
        print("next fire:", nf.isoformat() if nf else "(none)")
    else:
        print("Full crontab:")
        for ln in _read_crontab():
            print(" ", ln)
