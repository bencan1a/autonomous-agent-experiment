"""Plain-runner tests for instance_control (no pytest).

Prints PASS/FAIL per check and exits nonzero on any failure. Uses a temp
instance dir by pointing instance_control.INSTANCES_DIR at a tempdir.
"""

from __future__ import annotations

import sys
import tempfile
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


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="instctl-test-")
    instance_control.INSTANCES_DIR = Path(tmp)
    iid = "test-instance"
    # The instance dir always exists for a real instance; create it.
    (Path(tmp) / iid).mkdir(parents=True, exist_ok=True)

    # 1. Default state when no file.
    check("1a default is_paused False", instance_control.is_paused(iid) is False)
    state = instance_control.read_control(iid)
    check("1b default read_control", state == {"paused": False, "paused_at": None, "reason": None, "resume_note": None})

    # 2. set_paused -> paused True, paused_at set, persists across fresh read.
    new = instance_control.set_paused(iid, reason="operator maintenance pause")
    check("2a set_paused returns paused True", new["paused"] is True)
    check("2b set_paused paused_at set", bool(new["paused_at"]))
    check("2c is_paused True", instance_control.is_paused(iid) is True)
    reread = instance_control.read_control(iid)
    check("2d persists across fresh read", reread["paused"] is True and bool(reread["paused_at"]))

    # 3. clear_paused returns prior (with paused_at), then not paused.
    prior = instance_control.clear_paused(iid)
    check("3a clear_paused returns prior paused True", prior["paused"] is True)
    check("3b prior carries paused_at", bool(prior["paused_at"]))
    check("3c is_paused False after clear", instance_control.is_paused(iid) is False)

    # 4. set_resume_note / consume_resume_note: consume returns note then None;
    #    paused flag untouched.
    instance_control.set_paused(iid, reason=None)  # pause so we can verify flag is untouched
    instance_control.set_resume_note(iid, "hello there")
    paused_before = instance_control.is_paused(iid)
    got = instance_control.consume_resume_note(iid)
    check("4a consume returns the note", got == "hello there")
    got2 = instance_control.consume_resume_note(iid)
    check("4b second consume returns None", got2 is None)
    check("4c paused flag untouched by consume", instance_control.is_paused(iid) == paused_before)

    # 5. Corrupt control.json -> read_control returns defaults (no exception).
    instance_control.control_path(iid).write_text("{ this is not json", encoding="utf-8")
    try:
        corrupt = instance_control.read_control(iid)
        ok = corrupt == {"paused": False, "paused_at": None, "reason": None, "resume_note": None}
    except Exception:
        ok = False
    check("5 corrupt file -> defaults, no exception", ok)

    # Orchestrator guard: logic-only check (we do NOT run a real session). The
    # guard simply branches on instance_control.is_paused(instance.id). Verify
    # that predicate behaves as the guard expects without invoking orchestrator.
    instance_control.set_paused(iid, reason="x")
    check("6a guard predicate True when paused", instance_control.is_paused(iid) is True)
    instance_control.clear_paused(iid)
    check("6b guard predicate False when not paused", instance_control.is_paused(iid) is False)

    print()
    if _failures:
        print(f"{_failures} check(s) FAILED")
        return 1
    print("All checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
