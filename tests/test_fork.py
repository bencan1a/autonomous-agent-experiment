"""Verification tests for the instance-fork feature (instance_manager.fork_instance)
and its concurrency-safety layer (lockfile.file_lock, cron_control locking,
instances_common.registry_txn).

Fully offline: NO real network, NO Slack/Anthropic, NO real crontab. EpisodicStore
and ResearchStore are REAL (sqlite) pointed at a temp instances/ tree; the module
path constants and cron are monkeypatched at temp scope.

Run:
    ./venv/bin/python tests/test_fork.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cron_control  # noqa: E402
import instance_manager as im  # noqa: E402
import instances_common as ic  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402
from research.store import ResearchStore  # noqa: E402

UTC = timezone.utc
PARENT = "v4-continuous"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class RecordingCron:
    """Stand-in for cron_control: records calls, never touches the crontab."""
    MIN_INTERVAL_MINUTES = 30

    def __init__(self):
        self.calls: list[tuple] = []

    def clear_instance(self, iid):
        self.calls.append(("clear", iid))

    def remove_instance_entries(self, iid):
        # instance_control.pause() clears the schedule via this; record it as a
        # 'clear' so existing assertions hold.
        self.calls.append(("clear", iid))
        return 0

    def install_instance_one_shot(self, iid, *, minutes_from_now, command=None):
        self.calls.append(("install", iid, minutes_from_now))
        return "fake"

    def next_fire_at(self, iid):
        return None

    def find(self, kind):
        return [c for c in self.calls if c[0] == kind]


class FakeHistoryResp:
    def __init__(self, messages):
        self.data = {"messages": messages}


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

class fork_env:
    """Redirect all path constants + cron to a temp tree for the duration."""

    def __init__(self, tmp: Path, *, cron=None):
        self.tmp = tmp
        self.cron = cron or RecordingCron()
        self._saved: dict = {}

    def __enter__(self):
        self._saved = {
            (ic, "INSTANCES_DIR"): ic.INSTANCES_DIR,
            (ic, "REGISTRY_PATH"): ic.REGISTRY_PATH,
            (ic, "REGISTRY_LOCK"): ic.REGISTRY_LOCK,
            (im, "AGENT_ROOT"): im.AGENT_ROOT,
            (im, "cron"): im.cron,
            # State transitions now route cron through instance_control -> the
            # cron_control MODULE, so patch the module funcs too (else real crontab).
            (cron_control, "install_instance_one_shot"): cron_control.install_instance_one_shot,
            (cron_control, "remove_instance_entries"): cron_control.remove_instance_entries,
        }
        ic.INSTANCES_DIR = self.tmp / "instances"
        ic.REGISTRY_PATH = self.tmp / "registry.json"
        ic.REGISTRY_LOCK = self.tmp / "registry.json.lock"
        im.AGENT_ROOT = self.tmp
        im.cron = self.cron
        cron_control.install_instance_one_shot = self.cron.install_instance_one_shot
        cron_control.remove_instance_entries = self.cron.remove_instance_entries
        (self.tmp / "experiments").mkdir(parents=True, exist_ok=True)
        (self.tmp / "instances").mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        for (obj, attr), val in self._saved.items():
            setattr(obj, attr, val)
        return False


def _seed_parent(tmp: Path) -> dict:
    """Create a realistic active parent instance. Returns reference values."""
    root = tmp / "instances" / PARENT
    cfg = ic.default_config(PARENT, "v4")
    cfg["status"] = "active"
    inst = ic.Instance(PARENT, root, cfg)
    inst.ensure_dirs()
    ic.save_config(PARENT, cfg)

    # workspace handoff
    (inst.workspace_dir / "AGENTS.md").write_text("# handoff\nthread: alpha\n", encoding="utf-8")

    # episodes 1..7 + meta + cost
    ep = EpisodicStore(inst.episodes_db)
    sid = ep.start_session(invocation_num=1, pid=123)
    for n in range(1, 8):
        ep.log_episode(
            invocation_num=n, current_focus=f"focus-{n}", actions_taken=["a"],
            decisions_made=None, internal_state="calm", journal_entry="j",
            next_invoke_minutes=60, raw_output="{}", tokens_in=10, tokens_out=10,
            cost_usd=0.01, session_id=sid,
        )
    ep.set_meta("start_date", "2026-05-01T00:00:00+00:00")
    ep.set_meta("last_seen_ben_dm_ts", "1700000000.000100")
    ep.add_cost(tokens_in=100, tokens_out=100, cost_usd=1.23)

    # research state: experiment_id-keyed rows (must be rewritten) + session-keyed
    # rows (must be preserved untouched).
    rs = ResearchStore(inst.episodes_db)  # creates the research_* tables
    spec_src = str(tmp / "experiments" / f"{PARENT}.md")
    con = sqlite3.connect(str(inst.episodes_db))
    now = datetime.now(UTC).isoformat()
    con.execute(
        "INSERT INTO research_prereg(experiment_id, spec_hash, created_at, status, spec_source) "
        "VALUES (?,?,?,?,?)", (PARENT, "hash123", now, "approved", spec_src))
    con.execute(
        "INSERT INTO research_cumulative(experiment_id, created_at, version) VALUES (?,?,?)",
        (PARENT, now, 1))
    con.execute(
        "INSERT INTO research_proposals(experiment_id, created_at, status) VALUES (?,?,?)",
        (PARENT, now, "open"))
    con.execute(
        "INSERT INTO research_notes(session_id, created_at, coding_status) VALUES (?,?,?)",
        (sid, now, "binding"))
    con.execute(
        "INSERT INTO research_kappa(session_id, created_at, metric, value) VALUES (?,?,?,?)",
        (sid, now, "fleiss", 0.7))
    con.commit()
    con.close()

    # experiments/<parent>.md formal spec
    (tmp / "experiments" / f"{PARENT}.md").write_text(
        "# v4\n\n## Specification (formal)\n\n```yaml\n"
        "spec_version: 1\nexperiment_id: v4-continuous\nhypotheses:\n"
        "  - id: H1\n    statement: x\n    predicted_evidence: y\n    falsifying_evidence: z\n"
        "```\n", encoding="utf-8")

    # registry: parent active
    with ic.registry_txn() as reg:
        reg["instances"][PARENT] = {
            "id": PARENT, "name": PARENT, "version": "v4", "status": "active",
            "created_at": now, "active": True, "last_wake": now,
        }
    return {
        "inst": inst,
        "next_inv": EpisodicStore(inst.episodes_db).next_invocation_num(),
        "cost_today": EpisodicStore(inst.episodes_db).cost_today(),
        "start_date": ep.get_meta("start_date"),
        "last_seen": ep.get_meta("last_seen_ben_dm_ts"),
    }


def _q(db, sql, args=()):
    con = sqlite3.connect(str(db))
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

def scenario_fork_transparency_and_lineage():
    """Children continue seamlessly; lineage + co-activation are correct."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with fork_env(tmp) as env:
            ref = _seed_parent(tmp)
            assert ref["next_inv"] == 8, ref["next_inv"]

            ids = im.fork_instance(PARENT, ["a", "b"], do_slack=False)
            assert ids == ["v4-continuous-8a", "v4-continuous-8b"], ids

            for cid in ids:
                cinst = ic.load_instance(cid)
                cep = EpisodicStore(cinst.episodes_db)
                # continuity: invocation counter, start_date, last_seen all preserved
                assert cep.next_invocation_num() == ref["next_inv"], cid
                assert cep.get_meta("start_date") == ref["start_date"], cid
                assert cep.get_meta("last_seen_ben_dm_ts") == ref["last_seen"], cid
                # budget spent-to-date carries over (caps continue, not reset)
                assert abs(cep.cost_today() - ref["cost_today"]) < 1e-9, cid
                # workspace handoff copied
                assert (cinst.workspace_dir / "AGENTS.md").exists(), cid

            # research id rewrite on child 8a
            cdb = ic.load_instance("v4-continuous-8a").episodes_db
            assert _q(cdb, "SELECT experiment_id FROM research_prereg")[0][0] == "v4-continuous-8a"
            assert _q(cdb, "SELECT experiment_id FROM research_cumulative")[0][0] == "v4-continuous-8a"
            assert _q(cdb, "SELECT experiment_id FROM research_proposals")[0][0] == "v4-continuous-8a"
            ss = _q(cdb, "SELECT spec_source FROM research_prereg")[0][0]
            assert ss.endswith("/experiments/v4-continuous-8a.md"), ss
            # session-keyed research rows untouched (still resolvable, unchanged)
            assert _q(cdb, "SELECT coding_status FROM research_notes")[0][0] == "binding"
            assert _q(cdb, "SELECT value FROM research_kappa")[0][0] == 0.7

            # experiments/<child>.md created with rewritten experiment_id
            child_spec = (tmp / "experiments" / "v4-continuous-8a.md").read_text()
            assert "experiment_id: v4-continuous-8a" in child_spec

            # registry lineage + co-activation
            reg = ic.load_registry()
            for cid, label in (("v4-continuous-8a", "a"), ("v4-continuous-8b", "b")):
                e = reg["instances"][cid]
                assert e["parent_id"] == PARENT and e["branch_label"] == label, e
                assert e["forked_at_invocation"] == 7, e
                assert e["fork_group"] == "v4-continuous@7:a.b", e
                assert e["status"] == "active", e
            # parent frozen (single status; no separate `active` flag)
            assert reg["instances"][PARENT]["status"] == "paused"
            assert set(ic.active_instance_ids()) == set(ids)

            # cron: installed both children, parent cleared, parent not scheduled
            assert {c[1] for c in env.cron.find("install")} == set(ids)
            assert ("clear", PARENT) in env.cron.calls
            assert PARENT not in {c[1] for c in env.cron.find("install")}
        return f"children continue at cycle 8; lineage+research rewrite+co-active OK"


def scenario_keep_parent():
    """--keep-parent leaves the parent co-active with the branches."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with fork_env(tmp):
            _seed_parent(tmp)
            ids = im.fork_instance(PARENT, ["a"], do_slack=False, freeze_parent=False)
            assert set(ic.active_instance_ids()) == set(ids) | {PARENT}
        return "parent stays active alongside branch a"


def scenario_dry_run_no_side_effects():
    """--dry-run computes ids but writes nothing."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with fork_env(tmp) as env:
            _seed_parent(tmp)
            ids = im.fork_instance(PARENT, ["a", "b"], do_slack=False, dry_run=True)
            assert ids == ["v4-continuous-8a", "v4-continuous-8b"], ids
            assert not (tmp / "instances" / "v4-continuous-8a").exists()
            assert not (tmp / "experiments" / "v4-continuous-8a.md").exists()
            assert "v4-continuous-8a" not in ic.load_registry()["instances"]
            assert env.cron.calls == []
        return "dry-run created no dirs/specs/registry/cron"


def scenario_quiesce_refuses_live_parent():
    """Forking refuses while a live orchestrator holds the parent lock."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with fork_env(tmp):
            _seed_parent(tmp)
            inst = ic.load_instance(PARENT)
            inst.lock_path.write_text(str(__import__("os").getpid()), encoding="utf-8")
            saved = im.lockfile._pid_is_orchestrator
            im.lockfile._pid_is_orchestrator = lambda pid: True  # pretend it's a live orch
            try:
                raised = False
                try:
                    im.fork_instance(PARENT, ["a"], do_slack=False)
                except RuntimeError:
                    raised = True
                assert raised, "expected refusal while parent orchestrator is live"
            finally:
                im.lockfile._pid_is_orchestrator = saved
        return "fork refused while parent session live"


def scenario_overrides_applied():
    """Per-branch config override is merged into the child config."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with fork_env(tmp):
            _seed_parent(tmp)
            ov = {"a": {"config": {"decay_hours": 24, "model": "claude-haiku-4-5-20251001"}}}
            ids = im.fork_instance(PARENT, ["a"], do_slack=False, overrides=ov)
            cfg = ic.load_instance(ids[0]).config
            assert cfg["decay_hours"] == 24 and cfg["model"].startswith("claude-haiku"), cfg
            # untouched knobs inherited from parent
            assert cfg["weekly_cost_cap_usd"] == 300, cfg
        return "branch a override (decay_hours/model) applied; rest inherited"


def scenario_registry_txn_no_lost_update():
    """Concurrent registry_txn writers do not clobber each other."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ic_saved = (ic.REGISTRY_PATH, ic.REGISTRY_LOCK)
        ic.REGISTRY_PATH = tmp / "registry.json"
        ic.REGISTRY_LOCK = tmp / "registry.json.lock"
        try:
            with ic.registry_txn() as reg:
                reg["instances"]["x"] = {"counter": 0}
            N = 20

            def bump():
                with ic.registry_txn() as reg:
                    c = reg["instances"]["x"]["counter"]
                    time.sleep(0.002)  # widen the race window
                    reg["instances"]["x"]["counter"] = c + 1

            def add(i):
                with ic.registry_txn() as reg:
                    reg["instances"][f"new{i}"] = {"id": f"new{i}"}

            ts = [threading.Thread(target=bump) for _ in range(N)]
            ts += [threading.Thread(target=add, args=(i,)) for i in range(5)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            reg = ic.load_registry()
            assert reg["instances"]["x"]["counter"] == N, reg["instances"]["x"]
            assert all(f"new{i}" in reg["instances"] for i in range(5)), reg
        finally:
            ic.REGISTRY_PATH, ic.REGISTRY_LOCK = ic_saved
        return f"{N} concurrent bumps + 5 adds all survived"


def scenario_crontab_lock_no_lost_line():
    """Concurrent install_instance_one_shot calls keep every instance's line."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        backing = tmp / "fake_crontab"
        backing.write_text("# @reboot dashboard\n", encoding="utf-8")
        saved = (cron_control._read_crontab, cron_control._write_crontab, cron_control.CRONTAB_LOCK)
        cron_control.CRONTAB_LOCK = tmp / ".crontab.lock"

        def fake_read():
            return backing.read_text(encoding="utf-8").splitlines()

        def fake_write(lines):
            time.sleep(0.003)  # widen the read-modify-write race window
            backing.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cron_control._read_crontab = fake_read
        cron_control._write_crontab = fake_write
        try:
            iids = [f"inst-{i}" for i in range(12)]
            ts = [threading.Thread(
                target=cron_control.install_instance_one_shot,
                args=(iid,), kwargs={"minutes_from_now": 30}) for iid in iids]
            [t.start() for t in ts]
            [t.join() for t in ts]
            final = backing.read_text(encoding="utf-8")
            missing = [iid for iid in iids if cron_control.instance_marker(iid) not in final]
            assert not missing, f"lost lines for: {missing}"
            assert "# @reboot dashboard" in final, "clobbered the untagged dashboard line"
        finally:
            cron_control._read_crontab, cron_control._write_crontab, cron_control.CRONTAB_LOCK = saved
        return "12 concurrent cron installs + untagged line all survived"


def scenario_replay_refetch_safety():
    """Bot-authored replayed messages are NOT re-fetched as new Ben messages."""
    from communications.slack_client import SlackClient
    sc = SlackClient(bot_token="x", ben_user_id="UBEN",
                     chat_channel="C1", notes_channel=None, mirror_channel=None)
    messages = [
        # replayed history, posted by the bot (note prefix + bot author)
        {"ts": "1700000100.0001", "text": "*[replay] Ben · t:*\nold ben line", "user": "UBOT"},
        {"ts": "1700000100.0002", "text": "*[replay] agent · t:*\nold agent line", "bot_id": "B1"},
        {"ts": "1700000100.0003", "text": "joined", "subtype": "channel_join", "user": "UBEN"},
        # a genuinely new message actually authored by Ben
        {"ts": "1700000100.0004", "text": "new real msg", "user": "UBEN"},
    ]
    sc._client = SimpleNamespace(conversations_history=lambda **kw: FakeHistoryResp(messages))
    got = sc.fetch_dms_from_ben(oldest_ts="0")
    assert [m["text"] for m in got] == ["new real msg"], got
    return "replayed (bot) lines filtered; only the real Ben message returned"


SCENARIOS = [
    ("fork transparency + lineage + research rewrite", scenario_fork_transparency_and_lineage),
    ("keep-parent co-activation", scenario_keep_parent),
    ("dry-run has no side effects", scenario_dry_run_no_side_effects),
    ("quiesce refuses live parent", scenario_quiesce_refuses_live_parent),
    ("per-branch overrides applied", scenario_overrides_applied),
    ("registry_txn no lost update", scenario_registry_txn_no_lost_update),
    ("crontab lock no lost line", scenario_crontab_lock_no_lost_line),
    ("slack replay re-fetch safety", scenario_replay_refetch_safety),
]


def main() -> int:
    passed = 0
    for label, fn in SCENARIOS:
        try:
            detail = fn()
            print(f"PASS  {label}  —  {detail}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"FAIL  {label}  —  {type(e).__name__}: {e}")
            traceback.print_exc()
    total = len(SCENARIOS)
    print(f"\n{passed}/{total} scenarios passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
