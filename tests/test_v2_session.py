"""Verification test for the v2 agent session loop (v2_session.py).

Fully offline: NO real network, NO Anthropic/Slack/Brave, NO embedding-model
load, NO real crontab. Everything external is monkeypatched. EpisodicStore is
REAL (sqlite) pointed at a temp instance dir.

Run:
    ./venv/bin/python -m pytest tests/test_v2_session.py -q
    (or, if pytest is unavailable)
    ./venv/bin/python tests/test_v2_session.py
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

# Make the project (and this tests dir) importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import (  # noqa: E402  shared version-neutral test scaffolding
    FakeBlock,
    FakeLockfile,
    FakeSemantic,
    RecordingCron,
    text_resp,
    tool_resp,
)

# Dummy env BEFORE importing v2_session (its _env() reads at session run time,
# but set early to be safe).
for _k in (
    "ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "SLACK_AGENT_CHANNEL_ID",
    "SLACK_OBSERVER_CHANNEL_ID", "SLACK_BEN_USER_ID", "BRAVE_API_KEY",
):
    os.environ[_k] = "x"

# The loop logs expected warnings (e.g. the slack=None path) via log.exception;
# silence the orchestrator logger so test output stays clean.
logging.getLogger("orchestrator.v2").setLevel(logging.CRITICAL)

import v2_session  # noqa: E402
import instances_common  # noqa: E402
from agent_tools.registry import ToolContext, dispatch  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

def end_tick_resp(*, end_session=True, next_invoke_minutes=None,
                  tick_focus="done", usage=None, **extra):
    inp = {"tick_focus": tick_focus, "end_session": end_session}
    if next_invoke_minutes is not None:
        inp["next_invoke_minutes"] = next_invoke_minutes
    inp.update(extra)
    return tool_resp([("end_tick", inp)], usage=usage)


class FakeAnthropicClient:
    def __init__(self, queue):
        self.queue = queue
        self.messages = self  # so client.messages.create works

    def create(self, **kwargs):
        if not self.queue:
            raise AssertionError(
                "FakeAnthropic queue exhausted: model called more times than scripted"
            )
        return self.queue.pop(0)


def make_fake_anthropic(queue):
    return SimpleNamespace(Anthropic=lambda **kw: FakeAnthropicClient(queue))


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

def _build_instance(tmp: Path, config_overrides: dict) -> instances_common.Instance:
    root = tmp / "instances" / "testinst"
    cfg = instances_common.default_config("testinst", "v2", model="claude-sonnet-4-6")
    cfg.update({
        "tick_interval_seconds": 0,
        "min_wake_hours": 2,
        "decay_hours": 72,
        "prompt_caching": True,
        "in_session_compaction": True,
    })
    cfg.update(config_overrides)
    inst = instances_common.Instance(id="testinst", root=root, config=cfg)
    inst.ensure_dirs()
    instances_common.save_config_path = None  # noop guard
    (root / "config.json").write_text("{}", encoding="utf-8")
    return inst


class Patches:
    """Context manager installing all v2_session monkeypatches."""

    def __init__(self, queue, *, slack=None, semantic=None):
        self.queue = queue
        self.slack = slack
        self.semantic = semantic or FakeSemantic()
        self.cron = RecordingCron()
        self.lock = FakeLockfile()
        self._saved = {}

    def __enter__(self):
        m = v2_session
        self._saved = {
            "anthropic": m.anthropic,
            "SlackClient": m.SlackClient,
            "BraveSearch": m.BraveSearch,
            "SemanticStore": m.SemanticStore,
            "cron_control": m.cron_control,
            "lockfile": m.lockfile,
            "registry_txn": m.registry_txn,
        }
        m.anthropic = make_fake_anthropic(self.queue)
        if self.slack is not None:
            m.SlackClient = lambda **kw: self.slack
        else:
            def _no_slack(**kw):
                raise RuntimeError("slack disabled in test")
            m.SlackClient = _no_slack
        m.BraveSearch = lambda *a, **kw: SimpleNamespace(search=lambda *a, **k: [])
        sem = self.semantic
        m.SemanticStore = lambda *a, **kw: sem
        m.cron_control = self.cron
        m.lockfile = self.lock

        from contextlib import contextmanager

        @contextmanager
        def _fake_registry_txn():
            yield {"instances": {}}  # throwaway: writes go nowhere in tests

        m.registry_txn = _fake_registry_txn
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            setattr(v2_session, k, v)
        return False


def _set_old(db_path: str, episode_id: int, hours: float):
    ts = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE episodes SET timestamp = ? WHERE id = ?", (ts, episode_id))
    conn.commit()
    conn.close()


def _insert_episode(ep: EpisodicStore, *, invocation_num, focus="f"):
    return ep.log_episode(
        invocation_num=invocation_num, current_focus=focus, actions_taken=[],
        decisions_made=None, internal_state=None, journal_entry=None,
        next_invoke_minutes=None, raw_output="{}", tokens_in=0, tokens_out=0,
        cost_usd=0.0,
    )


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

def scenario_1_early_end():
    """No enforcement — early end honored + logged."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        inst = _build_instance(tmp, {})
        queue = [end_tick_resp(end_session=True, next_invoke_minutes=45,
                               tick_focus="leaving")]
        with Patches(queue) as p:
            rc = v2_session.run_v2_session(inst)
        assert rc == 0, f"return code {rc}"
        assert len(queue) == 0, f"expected 1 response consumed, {len(queue)} left"

        ep = EpisodicStore(inst.episodes_db)
        rows = ep.recent_v2_sessions(1)
        assert len(rows) == 1, "no v2_sessions row"
        r = rows[0]
        assert r["num_ticks"] == 1, f"num_ticks={r['num_ticks']}"
        assert r["end_reason"] == "agent_ended", r["end_reason"]
        assert r["ended_early"] == 1, f"ended_early={r['ended_early']}"
        assert r["next_invoke_minutes"] == 45, r["next_invoke_minutes"]

        installs = p.cron.find("install_instance_one_shot")
        assert len(installs) == 1, f"installs={p.cron.calls}"
        assert installs[0][2]["minutes_from_now"] == 45, installs[0]
    return "min_wake logged-not-enforced; early end honored, next wake @45m"


def scenario_2_multi_tick():
    """Multi-tick then end."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        inst = _build_instance(tmp, {})
        queue = [
            # tick 1: a non-end_tick tool call, then end_tick(continue)
            tool_resp([("recent_episodes", {"n": 3})]),
            end_tick_resp(end_session=False, tick_focus="thinking"),
            # tick 2: end_tick(end session, no reschedule)
            end_tick_resp(end_session=True, next_invoke_minutes=None,
                          tick_focus="done"),
        ]
        with Patches(queue) as p:
            rc = v2_session.run_v2_session(inst)
        assert rc == 0
        assert len(queue) == 0, f"{len(queue)} responses left"

        ep = EpisodicStore(inst.episodes_db)
        r = ep.recent_v2_sessions(1)[0]
        assert r["num_ticks"] == 2, f"num_ticks={r['num_ticks']}"
        assert r["end_reason"] == "agent_ended", r["end_reason"]
        assert r["next_invoke_minutes"] is None, r["next_invoke_minutes"]

        eps = ep.all_episodes()
        sess_eps = [e for e in eps if e.get("session_id") == r["session_id"]]
        assert len(sess_eps) == 2, f"episodes for session={len(sess_eps)}"

        assert p.cron.find("clear_instance"), f"clear_instance not called: {p.cron.calls}"
        assert not p.cron.find("install_instance_one_shot"), "should not reschedule"
    return "2 ticks, 2 episodes, agent_ended, no reschedule -> clear_instance"


def scenario_3_decay():
    """Decay (unit): only old+unconsolidated deleted."""
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "ep.db")
        ep = EpisodicStore(db)
        id_old_unconsol = _insert_episode(ep, invocation_num=1, focus="old_unconsol")
        id_old_consol = _insert_episode(ep, invocation_num=2, focus="old_consol")
        id_recent = _insert_episode(ep, invocation_num=3, focus="recent")

        _set_old(db, id_old_unconsol, 100)
        _set_old(db, id_old_consol, 100)
        ep.mark_consolidated([id_old_consol])
        # recent stays recent

        deleted = v2_session.run_decay(ep, decay_hours=72)

        assert ep.get_episode(id_old_unconsol) is None, "old+unconsol should be deleted"
        assert ep.get_episode(id_old_consol) is not None, "old+consol must survive"
        assert ep.get_episode(id_recent) is not None, "recent must survive"
        assert len(deleted) == 1, f"deleted list len={len(deleted)}"
        assert deleted[0]["invocation_num"] == 1, deleted[0]
    return "only old+unconsolidated deleted; consolidated & recent survive"


def scenario_4_consolidation_survives_decay():
    """Consolidation survives decay."""
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "ep.db")
        ep = EpisodicStore(db)
        eid = _insert_episode(ep, invocation_num=1, focus="keep me")
        _set_old(db, eid, 100)

        sem = FakeSemantic()
        ctx = ToolContext(
            episodic=ep, semantic=sem, slack=None, brave=None, anthropic=None,
            session_id=1, invocation_num=1, agent_root=Path(td),
            workspace_dir=Path(td),
        )
        result = dispatch("consolidate", {"episode_ids": [eid]}, ctx)
        assert result["consolidated"] == [eid], result
        assert ep.get_episode(eid)["consolidated"] == 1, "not marked consolidated"

        deleted = v2_session.run_decay(ep, decay_hours=72)
        assert ep.get_episode(eid) is not None, "consolidated episode wrongly decayed"
        assert eid not in [d.get("invocation_num") for d in deleted]
        assert len(sem.added) == 1, f"add_episode calls={len(sem.added)}"
    return "consolidated episode survives decay; semantic.add_episode called once"


def scenario_5_budget_guard():
    """Budget guard trips at top of loop with cap 0.0 (zero responses consumed)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        inst = _build_instance(tmp, {"session_cost_cap_usd": 0.0})
        queue = [end_tick_resp(end_session=True, tick_focus="never reached")]
        with Patches(queue) as p:
            rc = v2_session.run_v2_session(inst)
        assert rc == 0
        assert len(queue) == 1, f"expected 0 responses consumed, {1 - len(queue)} used"

        ep = EpisodicStore(inst.episodes_db)
        r = ep.recent_v2_sessions(1)[0]
        assert r["end_reason"] == "session_cost_cap", r["end_reason"]
        assert r["num_ticks"] == 0, f"num_ticks={r['num_ticks']}"
        assert p.cron.find("clear_instance"), f"clear_instance not called: {p.cron.calls}"
    return "budget guard trips pre-tick; 0 responses consumed; instance paused"


def scenario_6_compaction():
    """Compaction (unit): transcript collapses to head + summary."""
    head = {"role": "user", "content": "HEAD CONTEXT"}
    messages = [head]
    for i in range(4):
        messages.append({"role": "assistant", "content": [FakeBlock("text", text=f"a{i}")]})
        messages.append({"role": "user", "content": f"u{i}"})

    client = FakeAnthropicClient([text_resp("SUMMARY")])
    v2_session.compact_session_transcript(messages, client, "claude-sonnet-4-6")

    assert len(messages) == 2, f"len={len(messages)}"
    assert messages[0] is head, "head not preserved"
    assert messages[1]["role"] == "user", messages[1]["role"]
    assert "SUMMARY" in messages[1]["content"], messages[1]["content"]
    return "transcript compacted to [head, user(SUMMARY)]"


SCENARIOS = [
    ("1: no enforcement — early end honored + logged", scenario_1_early_end),
    ("2: multi-tick then end", scenario_2_multi_tick),
    ("3: decay (unit)", scenario_3_decay),
    ("4: consolidation survives decay", scenario_4_consolidation_survives_decay),
    ("5: budget guard", scenario_5_budget_guard),
    ("6: compaction (unit)", scenario_6_compaction),
]


# --------------------------------------------------------------------------- #
# pytest entrypoints (if pytest present) + plain-script runner
# --------------------------------------------------------------------------- #

def test_scenario_1_early_end(): scenario_1_early_end()
def test_scenario_2_multi_tick(): scenario_2_multi_tick()
def test_scenario_3_decay(): scenario_3_decay()
def test_scenario_4_consolidation_survives_decay(): scenario_4_consolidation_survives_decay()
def test_scenario_5_budget_guard(): scenario_5_budget_guard()
def test_scenario_6_compaction(): scenario_6_compaction()


def _main():
    failures = 0
    for label, fn in SCENARIOS:
        try:
            detail = fn()
            print(f"PASS  {label}  —  {detail}")
        except Exception as e:
            failures += 1
            import traceback
            print(f"FAIL  {label}  —  {type(e).__name__}: {e}")
            traceback.print_exc()
    print()
    total = len(SCENARIOS)
    print(f"{total - failures}/{total} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
