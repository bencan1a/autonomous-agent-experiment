"""Plain-runner tests for instance_control (no pytest).

Prints PASS/FAIL per check and exits nonzero on any failure. Uses a temp
instance dir by pointing instance_control.INSTANCES_DIR at a tempdir.

Run-state (active/paused/archived) is owned by the registry, not control.json —
see instance_control.apply_status / current_status / is_paused. control.json is a
metadata sidecar holding only pause metadata + the agent-facing resume note. These
tests isolate the control.json side of the module: the registry write
(apply_status) and the crontab calls (cron_control) are stubbed out, so the suite
needs no real registry or crontab. The registry-integration path is exercised by
tests/test_state_model.py.
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import instance_control  # noqa: E402

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    status = "PASS" if cond else "FAIL"
    if not cond:
        _failures += 1
    print(f"[{status}] {name}")


def _install_stubs() -> tuple[list, types.SimpleNamespace]:
    """Stub the registry write (apply_status) and cron_control so pause()/start()
    exercise only their control.json side effects. Returns (apply_status calls,
    cron call recorder)."""
    status_calls: list = []
    instance_control.apply_status = lambda iid, status: status_calls.append((iid, status))

    cron = types.SimpleNamespace(calls=[])
    fake = types.ModuleType("cron_control")
    fake.remove_instance_entries = lambda iid: cron.calls.append(("remove", iid))
    fake.install_instance_one_shot = lambda iid, *, minutes_from_now: cron.calls.append(
        ("install", iid, minutes_from_now)
    )
    sys.modules["cron_control"] = fake
    return status_calls, cron


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="instctl-test-")
    instance_control.INSTANCES_DIR = Path(tmp)
    iid = "test-instance"
    (Path(tmp) / iid).mkdir(parents=True, exist_ok=True)

    DEFAULT = {"paused_at": None, "reason": None, "resume_note": None}

    # 1. Default control state when no file (no authoritative `paused` flag anymore).
    state = instance_control.read_control(iid)
    check("1a default read_control == DEFAULT_STATE", state == DEFAULT)
    check("1b DEFAULT_STATE has no 'paused' key", "paused" not in state)

    # 2. Corrupt control.json -> read_control returns defaults (no exception).
    instance_control.control_path(iid).write_text("{ this is not json", encoding="utf-8")
    try:
        corrupt = instance_control.read_control(iid)
        ok = corrupt == DEFAULT
    except Exception:
        ok = False
    check("2 corrupt file -> defaults, no exception", ok)

    # 3. is_paused reflects the registry status (current_status), not control.json.
    instance_control.current_status = lambda _iid: "paused"
    check("3a is_paused True when status paused", instance_control.is_paused(iid) is True)
    instance_control.current_status = lambda _iid: "active"
    check("3b is_paused False when status active", instance_control.is_paused(iid) is False)

    # 4. set_resume_note / consume_resume_note round-trip (live path used by v4/v5).
    instance_control.set_resume_note(iid, "hello there")
    check("4a consume returns the note", instance_control.consume_resume_note(iid) == "hello there")
    check("4b second consume returns None", instance_control.consume_resume_note(iid) is None)
    check("4c set_resume_note(None) then consume is None",
          (instance_control.set_resume_note(iid, None),
           instance_control.consume_resume_note(iid))[1] is None)

    # 5. pause(): records pause metadata in control.json + writes registry status
    #    'paused' + clears the schedule. (apply_status / cron stubbed.)
    status_calls, cron = _install_stubs()
    instance_control.pause(iid, reason="operator maintenance pause")
    paused_state = instance_control.read_control(iid)
    check("5a pause sets paused_at", bool(paused_state["paused_at"]))
    check("5b pause records reason", paused_state["reason"] == "operator maintenance pause")
    check("5c pause writes registry status 'paused'", (iid, "paused") in status_calls)
    check("5d pause clears the schedule", ("remove", iid) in cron.calls)

    # 6. start(): clears pause metadata, sets resume note, status 'active', schedules wake.
    status_calls.clear()
    cron.calls.clear()
    instance_control.start(iid, minutes_from_now=30, resume_note="back online")
    started = instance_control.read_control(iid)
    check("6a start clears paused_at", started["paused_at"] is None)
    check("6b start clears reason", started["reason"] is None)
    check("6c start sets resume_note", started["resume_note"] == "back online")
    check("6d start writes registry status 'active'", (iid, "active") in status_calls)
    check("6e start schedules the next wake", ("install", iid, 30) in cron.calls)
    check("6f resume_note survives until consumed", instance_control.consume_resume_note(iid) == "back online")

    print()
    if _failures:
        print(f"{_failures} check(s) FAILED")
        return 1
    print("All checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
