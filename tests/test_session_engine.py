"""Golden / contract tests for session_engine — the shared v2–v5 engine (no pytest).

Two jobs:
  1. Pin the engine's own contract: setup_session (returns a populated runtime;
     aborts on budget preflight), cost_caps_tripped (threshold behavior), and
     schedule_next_wake (install vs clear).
  2. Anti-contamination golden: assert every version is wired to the SAME shared
     engine object, so a future edit can't silently fork one experiment's
     plumbing. The per-version *behavior* (loop shape, prompt, tool spec, session
     record) is pinned by tests/test_v{2,3,4,5}_session.py.

Prints PASS/FAIL per check and exits nonzero on any failure.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.getLogger("orchestrator.engine").setLevel(logging.CRITICAL)

import session_engine  # noqa: E402
import v2_session  # noqa: E402
import v3_session  # noqa: E402
import v4_session  # noqa: E402
import v5_session  # noqa: E402
# Reuse v2's offline harness (its Patches patches session_engine's deps).
from test_v2_session import Patches, _build_instance  # noqa: E402

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")


def main() -> int:
    versions = {
        "v2": v2_session, "v3": v3_session, "v4": v4_session, "v5": v5_session,
    }

    # 1. Anti-contamination: every version shares the one engine implementation.
    for name, mod in versions.items():
        check(f"{name} uses shared setup_session", mod.setup_session is session_engine.setup_session)
        check(f"{name} uses shared cost_caps_tripped", mod.cost_caps_tripped is session_engine.cost_caps_tripped)
        check(f"{name} uses shared record_episode", mod.record_episode is session_engine.record_episode)
        check(f"{name} uses shared finalize_stats", mod.finalize_stats is session_engine.finalize_stats)
        check(f"{name} uses shared schedule_next_wake", mod.schedule_next_wake is session_engine.schedule_next_wake)
        check(f"{name} re-exports shared run_decay", mod.run_decay is session_engine.run_decay)

    # 2. setup_session returns a populated runtime with config-derived params.
    with tempfile.TemporaryDirectory() as td:
        inst = _build_instance(Path(td), {})
        with Patches([]):
            rt = session_engine.setup_session(inst)
        check("setup_session returns a runtime", rt is not None)
        check("rt.model == instance.model", rt.model == inst.model)
        check("rt.session_id is a real id", isinstance(rt.session_id, int) and rt.session_id > 0)
        check("rt.ctx wired to the session id", rt.ctx.session_id == rt.session_id)
        check("rt.decayed is a list", isinstance(rt.decayed, list))
        check("rt.caching True for a non-OpenRouter model", rt.caching is True)
        check("rt.decay_hours from config",
              rt.decay_hours == float(inst.config.get("decay_hours", 72)))
        check("rt.compaction_threshold from config",
              rt.compaction_threshold == int(inst.config.get(
                  "compaction_token_threshold", session_engine.DEFAULT_COMPACTION_TOKENS)))

    # 3. Budget preflight aborts the session (returns None).
    with tempfile.TemporaryDirectory() as td:
        inst = _build_instance(Path(td), {"daily_cost_cap_usd": 0})
        with Patches([]):
            rt = session_engine.setup_session(inst)
        check("setup_session returns None when the daily cap is already hit", rt is None)

    # 4. cost_caps_tripped threshold behavior.
    with tempfile.TemporaryDirectory() as td:
        inst = _build_instance(Path(td), {"session_cost_cap_usd": 5})
        with Patches([]):
            rt = session_engine.setup_session(inst)
            check("cost_caps_tripped None below all caps",
                  session_engine.cost_caps_tripped(rt, 0.0) is None)
            check("cost_caps_tripped 'session_cost_cap' at the session cap",
                  session_engine.cost_caps_tripped(rt, 5.0) == "session_cost_cap")

    # 5. schedule_next_wake: an int installs a one-shot; None clears the entry.
    with tempfile.TemporaryDirectory() as td:
        inst = _build_instance(Path(td), {})
        with Patches([]) as p:
            rt = session_engine.setup_session(inst)
            session_engine.schedule_next_wake(rt, 45)
            installs = p.cron.find("install_instance_one_shot")
            check("schedule_next_wake(45) installs a one-shot at 45m",
                  bool(installs) and installs[-1][2]["minutes_from_now"] == 45)
            session_engine.schedule_next_wake(rt, None)
            check("schedule_next_wake(None) clears the cron entry",
                  bool(p.cron.find("clear_instance")))

    # 6. A mid-session operator pause is honored: no reschedule, cron cleared
    #    (guards against the 'paused but counting down' orphan).
    import instances_common
    with tempfile.TemporaryDirectory() as td:
        inst = _build_instance(Path(td), {})
        with Patches([]) as p:
            rt = session_engine.setup_session(inst)
            saved_load_registry = instances_common.load_registry
            instances_common.load_registry = lambda: {"instances": {inst.id: {"status": "paused"}}}
            try:
                session_engine.schedule_next_wake(rt, 45)
            finally:
                instances_common.load_registry = saved_load_registry
            check("paused mid-session -> schedule_next_wake installs nothing",
                  not p.cron.find("install_instance_one_shot"))
            check("paused mid-session -> schedule_next_wake clears the cron entry",
                  bool(p.cron.find("clear_instance")))

    print()
    if _failures:
        print(f"{_failures} check(s) FAILED")
        return 1
    print("All checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
