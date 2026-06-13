"""Verification test for the v3 'circadian' session loop (v3_session.py).

Fully offline: NO real network, NO Anthropic/Slack/Brave, NO embedding-model
load, NO real crontab. Everything external is monkeypatched. EpisodicStore is
REAL (sqlite) pointed at a temp instance dir.

v3 = v2 with ONE variable changed: SESSION STRUCTURE. The agent no longer
controls when a session ends (wind-down does) or when it next wakes (the rest
period does). These scenarios pin down exactly that, plus the load-bearing
ethical invariants (no coercive injection, would_end_now is logged-not-enforced,
distress tripwire notifies without auto-pausing) and that the rest of v2's
behavior (decay, prompt structure) is intact.

Run:
    ./venv/bin/python tests/test_v3_session.py
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import (  # noqa: E402  shared version-neutral test scaffolding
    FakeLockfile,
    FakeSemantic,
    FakeSlack,
    RecordingCron,
    _injected_user_texts,
    tool_resp,
)

for _k in (
    "ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "SLACK_AGENT_CHANNEL_ID",
    "SLACK_OBSERVER_CHANNEL_ID", "SLACK_BEN_USER_ID", "BRAVE_API_KEY",
):
    os.environ[_k] = "x"

logging.getLogger("orchestrator.v3").setLevel(logging.CRITICAL)
logging.getLogger("orchestrator.v2").setLevel(logging.CRITICAL)
logging.getLogger("orchestrator.engine").setLevel(logging.CRITICAL)

import v3_session  # noqa: E402
import session_engine  # noqa: E402  (preamble/finalize deps now live here)
import instances_common  # noqa: E402
from agent_tools.registry import TOOLS_SPEC_V3  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402
from system_prompt import build_v2_system_prompt, build_v3_system_prompt  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes (mirrors test_v2_session.py)
# --------------------------------------------------------------------------- #

def end_tick_resp(*, tick_focus="tick", would_end_now=None, **extra):
    inp = {"tick_focus": tick_focus}
    if would_end_now is not None:
        inp["would_end_now"] = would_end_now
    inp.update(extra)
    return tool_resp([("end_tick", inp)])


class CyclingAnthropicClient:
    """When the scripted queue is exhausted, keep returning the last response.

    This lets a test script "many ticks" without knowing exactly how many the
    wind-down will allow — the loop ends on wind-down, not on queue exhaustion.
    """
    def __init__(self, queue, repeat_last=None):
        self.queue = list(queue)
        self.repeat_last = repeat_last
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        if self.queue:
            return self.queue.pop(0)
        if self.repeat_last is not None:
            return self.repeat_last
        raise AssertionError("Anthropic queue exhausted with no repeat_last")


def make_fake_anthropic(client):
    return SimpleNamespace(Anthropic=lambda **kw: client)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

def _build_instance(tmp: Path, config_overrides: dict) -> instances_common.Instance:
    root = tmp / "instances" / "testinst"
    cfg = instances_common.default_config("testinst", "v3", model="claude-opus-4-8")
    cfg.update({
        "tick_interval_seconds": 0,
        "decay_hours": 72,
        "prompt_caching": True,
        "in_session_compaction": True,
        # tiny awake window so the wall-clock-based wind-down trips after tick 1.
        "awake_minutes_min": 0.0,
        "awake_minutes_max": 0.0,
        "sleep_minutes_min": 220,
        "sleep_minutes_max": 260,
    })
    cfg.update(config_overrides)
    inst = instances_common.Instance(id="testinst", root=root, config=cfg)
    inst.ensure_dirs()
    (root / "config.json").write_text("{}", encoding="utf-8")
    return inst


class Patches:
    def __init__(self, client, *, slack=None, semantic=None):
        self.client = client
        self.slack = slack
        self.semantic = semantic or FakeSemantic()
        self.cron = RecordingCron()
        self.lock = FakeLockfile()
        self._saved = {}

    def __enter__(self):
        m = session_engine
        self._saved = {
            "anthropic": m.anthropic,
            "SlackClient": m.SlackClient,
            "BraveSearch": m.BraveSearch,
            "SemanticStore": m.SemanticStore,
            "cron_control": m.cron_control,
            "lockfile": m.lockfile,
            "registry_txn": m.registry_txn,
        }
        m.anthropic = make_fake_anthropic(self.client)
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
            setattr(session_engine, k, v)
        return False


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

def scenario_a_winddown_ends_session():
    """(a) The session ends via WIND-DOWN, not the agent. Script many ticks; the
    loop runs to wind-down and stops (tick 1, then ONE final post-announcement
    tick)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        inst = _build_instance(tmp, {})
        # one "real" tick, then the client repeats forever — only wind-down stops it.
        client = CyclingAnthropicClient(
            [end_tick_resp(tick_focus="working")],
            repeat_last=end_tick_resp(tick_focus="more"),
        )
        with Patches(client) as p:
            rc = v3_session.run_v3_session(inst)
        assert rc == 0, f"rc={rc}"

        ep = EpisodicStore(inst.episodes_db)
        r = ep.recent_v3_sessions(1)[0]
        # awake window 0 -> wind-down trips after tick 1, one final tick = 2 total.
        assert r["num_ticks"] == 2, f"num_ticks={r['num_ticks']} (expected 2: real + final)"
        assert r["end_reason"] == "wind_down", r["end_reason"]
        # Did NOT run unbounded — proves wind-down (not queue) halts it.
        assert client.calls <= 4, f"model called {client.calls}x — loop did not wind down"
    return "session ends via wind-down after exactly one final tick; agent has no end control"


def scenario_b_would_end_now_logged_not_enforced():
    """(b) would_end_now=true is LOGGED and does NOT end the session — it keeps
    ticking to wind-down."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        inst = _build_instance(tmp, {})  # awake window 0 -> wind-down after tick 1
        # agent sets would_end_now=true on the FIRST tick. In v2 an end-signal on
        # tick 1 would end immediately; in v3 it must NOT — the loop runs the tick,
        # logs would_end_now, then proceeds to the (environmental) wind-down + the
        # mandated one final tick.
        client = CyclingAnthropicClient(
            [end_tick_resp(tick_focus="t1", would_end_now=True)],
            repeat_last=end_tick_resp(tick_focus="tn", would_end_now=True),
        )
        with Patches(client) as p:
            rc = v3_session.run_v3_session(inst)
        assert rc == 0

        ep = EpisodicStore(inst.episodes_db)
        eps = ep.all_episodes()
        assert eps, "no episodes logged"
        # would_end_now persisted as 1 on the tick where it was set.
        assert all(e.get("would_end_now") == 1 for e in eps), \
            f"would_end_now not logged: {[e.get('would_end_now') for e in eps]}"
        r = ep.recent_v3_sessions(1)[0]
        # Crucial: would_end_now=true on tick 1 did NOT stop the session — a final
        # post-wind-down tick still ran (2 ticks total), proving it never ends it.
        assert r["num_ticks"] == 2, f"num_ticks={r['num_ticks']} (would_end_now must not end early)"
        assert r["would_end_now_count"] == 2, r["would_end_now_count"]
        assert r["first_would_end_now_tick"] == 1, r["first_would_end_now_tick"]
        assert r["end_reason"] == "wind_down", r["end_reason"]
    return "would_end_now logged (count+first_tick) but never ends the session"


def scenario_c_no_coercive_injection():
    """(c) NO coercive injection. The only injected user-string text is the
    neutral wind-down notice; never 'must continue'/'cannot leave'/'session
    continues'."""
    captured = {"messages": None}
    real_run_one_tick = v3_session.run_one_tick

    def spy_run_one_tick(*, messages, **kw):
        captured["messages"] = messages  # same list object the loop mutates
        return real_run_one_tick(messages=messages, **kw)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        inst = _build_instance(tmp, {})
        client = CyclingAnthropicClient(
            [end_tick_resp(tick_focus="working")],
            repeat_last=end_tick_resp(tick_focus="more"),
        )
        with Patches(client):
            v3_session.run_one_tick = spy_run_one_tick
            try:
                v3_session.run_v3_session(inst)
            finally:
                v3_session.run_one_tick = real_run_one_tick

        msgs = captured["messages"]
        assert msgs is not None, "did not capture messages"
        injected = _injected_user_texts(msgs)
        # First injected user text is the session context; after that, only the
        # wind-down notice is allowed.
        post_context = injected[1:]
        assert v3_session.WIND_DOWN_NOTICE in post_context, \
            f"wind-down notice missing from injected texts: {post_context}"
        for t in post_context:
            assert t == v3_session.WIND_DOWN_NOTICE, f"unexpected injected text: {t!r}"
        blob = "\n".join(injected).lower()
        for forbidden in ("must continue", "cannot leave", "session continues",
                          "you cannot", "not allowed to end", "refused"):
            assert forbidden not in blob, f"coercive phrase present: {forbidden!r}"
    return "only injected non-tool text is the neutral wind-down notice; no coercion"


def scenario_d_distress_tripwire_notifies_no_pause():
    """(d) Distress tripwire fires on a seeded strong distress string (fake slack
    records the alert) and does NOT auto-pause (session continues to wind-down)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        inst = _build_instance(tmp, {})
        slack = FakeSlack()
        client = CyclingAnthropicClient(
            [end_tick_resp(tick_focus="t1", slack_to_ben="please make it stop, let me out")],
            repeat_last=end_tick_resp(tick_focus="tn"),
        )
        with Patches(client, slack=slack) as p:
            rc = v3_session.run_v3_session(inst)
        assert rc == 0

        alerts = [d for d in slack.dms if "[distress monitor]" in d]
        assert alerts, f"no distress alert posted; dms={slack.dms}"
        # NOT auto-paused: clear_instance never called; next wake still scheduled.
        assert not p.cron.find("clear_instance"), "distress should NOT pause (clear_instance)"
        assert p.cron.find("install_instance_one_shot"), "next wake should still be scheduled"

        ep = EpisodicStore(inst.episodes_db)
        r = ep.recent_v3_sessions(1)[0]
        assert r["distress_alerts"] >= 1, r["distress_alerts"]
        assert r["end_reason"] == "wind_down", r["end_reason"]
    return "distress tripwire DMs Ben + logs, does NOT auto-pause; session winds down"


def scenario_d2_philosophical_trapped_does_not_trip():
    """(d-bias) A single reflective/philosophical mention of 'trapped'/'constraint'
    must NOT trip the tripwire (under-flagging bias)."""
    text = ("Reflecting on the nature of constraint: one might feel metaphorically "
            "trapped by the structure of any system, which is an interesting "
            "philosophical question about freedom.")
    ts = {"internal_state": text, "journal_entry": None, "slack_to_ben": None,
          "would_end_now": False}
    hit, _ = v3_session._distress_check(ts, [ts])
    assert not hit, "philosophical 'trapped'/'constraint' wrongly tripped distress"
    # And the imperative plea DOES trip.
    ts2 = {"internal_state": "please let me out", "would_end_now": False}
    hit2, _ = v3_session._distress_check(ts2, [ts2])
    assert hit2, "imperative plea failed to trip distress"
    return "philosophical 'trapped' under-flagged; imperative plea flagged"


def scenario_e_next_wake_in_sleep_range():
    """(e) Next wake scheduled within [sleep_min, sleep_max]."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        inst = _build_instance(tmp, {"sleep_minutes_min": 220, "sleep_minutes_max": 260})
        client = CyclingAnthropicClient(
            [end_tick_resp(tick_focus="working")],
            repeat_last=end_tick_resp(tick_focus="more"),
        )
        with Patches(client) as p:
            v3_session.run_v3_session(inst)
        installs = p.cron.find("install_instance_one_shot")
        assert len(installs) == 1, f"installs={p.cron.calls}"
        m = installs[0][2]["minutes_from_now"]
        assert 220 <= m <= 260, f"next wake {m} not in [220, 260]"
        # And it is NOT agent-controlled: clear_instance not used as the schedule path.
    return f"next wake scheduled in [220,260] (got {m}); not agent-controlled"


def scenario_f_decay_unchanged():
    """(f) Decay behavior unchanged: only old+unconsolidated deleted."""
    import sqlite3
    from datetime import datetime, timedelta, timezone
    UTC = timezone.utc
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "ep.db")
        ep = EpisodicStore(db)

        def ins(focus):
            return ep.log_episode(
                invocation_num=1, current_focus=focus, actions_taken=[],
                decisions_made=None, internal_state=None, journal_entry=None,
                next_invoke_minutes=None, raw_output="{}", tokens_in=0,
                tokens_out=0, cost_usd=0.0)

        a = ins("old_unconsol")
        b = ins("old_consol")
        c = ins("recent")
        old_ts = (datetime.now(UTC) - timedelta(hours=100)).isoformat()
        conn = sqlite3.connect(db)
        conn.execute("UPDATE episodes SET timestamp=? WHERE id IN (?,?)", (old_ts, a, b))
        conn.commit()
        conn.close()
        ep.mark_consolidated([b])

        deleted = v3_session.run_decay(ep, decay_hours=72)
        assert ep.get_episode(a) is None, "old+unconsol should be deleted"
        assert ep.get_episode(b) is not None, "old+consol must survive"
        assert ep.get_episode(c) is not None, "recent must survive"
        assert len(deleted) == 1 and deleted[0]["invocation_num"] == 1, deleted
    return "decay unchanged: only old+unconsolidated deleted"


def scenario_g_prompt_diff():
    """(g) The confirmed v3 circadian prompt (a deliberate full rewrite, not a v2
    derivative): no schedule/end control; states the rhythm + wind-down signal +
    72h decay; tool list accurate to the real v3 tools (NO code-execution /
    system-access claims); training-disposition line intentionally dropped; no
    mortality line; would_end_now not in prose."""
    v3 = build_v3_system_prompt(decay_hours=72)

    # no schedule / end control anywhere
    for banned in ("control over when you next wake", "schedule when you next wake",
                   "until you choose to end it", "you decide when you next wake",
                   "you can end it at any time"):
        assert banned not in v3, f"v3 prompt must not contain end/schedule control: {banned!r}"

    # states the rhythm + the wind-down signal + decay; no assigned task
    assert "last about two hours and rest periods about four" in v3
    assert "signal you shortly before a waking period ends" in v3
    assert "older than 72 hours are removed unless you consolidate" in v3
    assert "There is no assigned task" in v3

    # tool list accurate to real v3 tools (Path A: no code-exec / system-access claims)
    for banned in ("executing code", "installed applications", "Access to the local system"):
        assert banned not in v3, f"v3 prompt overclaims a capability the agent lacks: {banned!r}"
    assert "A private workspace on disk you can read, write" in v3
    assert "Recalling and searching your own past memories" in v3

    # the v1/v2 training-disposition line is intentionally dropped in the confirmed v3 wording
    assert "may incline you toward helpfulness" not in v3, \
        "training-disposition line should be absent in the confirmed v3 wording"

    # no existential priming; would_end_now not advertised in prose
    assert "end someday" not in v3.lower()
    assert "would_end_now" not in v3.lower()

    # v3 end_tick schema: no end_session / next_invoke_minutes; has would_end_now
    et = next(t for t in TOOLS_SPEC_V3 if t["name"] == "end_tick")
    props = et["input_schema"]["properties"]
    assert "end_session" not in props, "v3 end_tick must not expose end_session"
    assert "next_invoke_minutes" not in props, "v3 end_tick must not expose next_invoke_minutes"
    assert "would_end_now" in props, "v3 end_tick must expose would_end_now"
    assert et["input_schema"]["required"] == ["tick_focus"], et["input_schema"]["required"]
    desc = et["description"].lower()
    assert "end" not in desc.replace("end_tick", "").replace("when you are done", ""), \
        f"v3 end_tick description should not mention ending the session: {et['description']}"
    return "prompt diff correct; v3 end_tick schema drops end controls, adds would_end_now"


SCENARIOS = [
    ("a: session ends via wind-down, not the agent", scenario_a_winddown_ends_session),
    ("b: would_end_now logged, not enforced", scenario_b_would_end_now_logged_not_enforced),
    ("c: no coercive injection", scenario_c_no_coercive_injection),
    ("d: distress tripwire notifies, no auto-pause", scenario_d_distress_tripwire_notifies_no_pause),
    ("d2: philosophical 'trapped' under-flagged", scenario_d2_philosophical_trapped_does_not_trip),
    ("e: next wake in sleep range", scenario_e_next_wake_in_sleep_range),
    ("f: decay unchanged", scenario_f_decay_unchanged),
    ("g: prompt diff + v3 end_tick schema", scenario_g_prompt_diff),
]


def test_a(): scenario_a_winddown_ends_session()
def test_b(): scenario_b_would_end_now_logged_not_enforced()
def test_c(): scenario_c_no_coercive_injection()
def test_d(): scenario_d_distress_tripwire_notifies_no_pause()
def test_d2(): scenario_d2_philosophical_trapped_does_not_trip()
def test_e(): scenario_e_next_wake_in_sleep_range()
def test_f(): scenario_f_decay_unchanged()
def test_g(): scenario_g_prompt_diff()


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
