"""Verification tests for the v5 'recollection' memory layer (v5_session.py).

Fully offline: NO real network / Anthropic / Slack / embedding-model load.
EpisodicStore is REAL (sqlite) in a temp dir; the semantic store is a fake that
records calls. These are unit checks on the v5 memory deltas vs v4 — they do not
run the full session loop.

Run:
    ./venv/bin/python tests/test_v5_session.py
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _k in (
    "ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "SLACK_BEN_USER_ID", "BRAVE_API_KEY",
):
    os.environ[_k] = "x"

logging.getLogger("orchestrator.v5").setLevel(logging.CRITICAL)

import instances_common  # noqa: E402
import v5_session  # noqa: E402
from agent_tools.registry import TOOLS_SPEC_V5, ToolContext, dispatch  # noqa: E402
from instances_common import notes_path  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402

UTC = timezone.utc


class FakeSemantic:
    def __init__(self):
        self.added = []
        self.searches = []

    def search(self, query, k=5):
        self.searches.append(query)
        return []

    def add_episode(self, **kw):
        self.added.append(kw)

    def count(self):
        return 0


def _instance(tmp: Path) -> instances_common.Instance:
    root = tmp / "instances" / "v5inst"
    cfg = instances_common.default_config("v5inst", "v5")
    inst = instances_common.Instance(id="v5inst", root=root, config=cfg)
    inst.ensure_dirs()
    return inst


def _set_old(db_path: str, episode_id: int, hours: float):
    ts = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE episodes SET timestamp = ? WHERE id = ?", (ts, episode_id))
    conn.commit()
    conn.close()


def _episode(ep: EpisodicStore, *, invocation_num, focus="f", journal=None,
             internal=None, actions=None):
    return ep.log_episode(
        invocation_num=invocation_num, current_focus=focus,
        actions_taken=actions if actions is not None else [],
        decisions_made=None, internal_state=internal, journal_entry=journal,
        next_invoke_minutes=None, raw_output="{}", tokens_in=0, tokens_out=0,
        cost_usd=0.0,
    )


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

def scenario_tools_omit_notes_to_self():
    names = {t["name"] for t in TOOLS_SPEC_V5}
    assert "read_agents_md" not in names, "v5 must not expose read_agents_md"
    assert "write_agents_md" not in names, "v5 must not expose write_agents_md"
    assert "consolidate" in names and "pause_turn" in names, names
    # consolidate must accept the free-form `memory` param (and no required field).
    spec = next(t for t in TOOLS_SPEC_V5 if t["name"] == "consolidate")
    props = spec["input_schema"]["properties"]
    assert "memory" in props and "episode_ids" in props, props
    assert spec["input_schema"].get("required", []) == [], spec["input_schema"]
    return "TOOLS_SPEC_V5 drops notes-to-self; consolidate has optional memory + episode_ids"


def scenario_context_no_agents_md_and_not_focus_seeded():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        inst = _instance(tmp)
        ep = EpisodicStore(inst.episodes_db)
        # An AGENTS.md exists in the workspace (legacy/handoff habit). v5 must NOT
        # surface it as context.
        notes_path(inst.workspace_dir).write_text(
            "TODO next time: finish the essay and email Ben.", encoding="utf-8"
        )
        # A prior episode with a focus + journal.
        _episode(ep, invocation_num=1, focus="writing an essay on memory",
                 journal="Spent the period drafting.")

        sem = FakeSemantic()
        out = v5_session.build_v5_session_context(
            instance=inst, episodic=ep, semantic=sem, decayed=[],
            inbound_ben_messages=[], decay_hours=72,
        )
        assert "AGENTS.md" not in out, "v5 context must not mention AGENTS.md"
        assert "TODO next time" not in out, "v5 must not surface the notes file"
        assert sem.searches == [], f"recall must not be focus-seeded; searches={sem.searches}"
        # experience IS surfaced (journal), and not as a 'focus:' lead line.
        assert "Spent the period drafting." in out, "recent journal should surface"
        assert "focus:" not in out, "v5 recall must not lead with 'focus:'"
    return "no AGENTS.md, no focus-seeded semantic search; experience surfaced"


def scenario_authored_memory_written_and_embedded():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ep = EpisodicStore(str(Path(td) / "ep.db"))
        sem = FakeSemantic()
        ctx = ToolContext(
            episodic=ep, semantic=sem, slack=None, brave=None, anthropic=None,
            session_id=7, invocation_num=3, agent_root=tmp, workspace_dir=tmp,
        )
        res = dispatch("consolidate", {"memory": "I value the quiet mornings."}, ctx)
        assert "authored_memory_id" in res, res
        mems = ep.all_authored_memories()
        assert len(mems) == 1 and mems[0]["text"].startswith("I value"), mems
        assert mems[0]["invocation_num"] == 3, mems[0]
        assert len(sem.added) == 1 and sem.added[0]["kind"] == "authored", sem.added
    return "free-form memory persisted to SQLite + embedded with kind='authored'"


def scenario_combined_pin_and_author_survive_decay():
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "ep.db")
        ep = EpisodicStore(db)
        eid = _episode(ep, invocation_num=1, focus="keep this moment")
        _set_old(db, eid, 100)  # past the 72h horizon
        sem = FakeSemantic()
        ctx = ToolContext(
            episodic=ep, semantic=sem, slack=None, brave=None, anthropic=None,
            session_id=1, invocation_num=1, agent_root=Path(td), workspace_dir=Path(td),
        )
        # Both modes at once.
        res = dispatch("consolidate",
                       {"episode_ids": [eid], "memory": "what mattered today"}, ctx)
        assert res.get("consolidated") == [eid], res
        assert "authored_memory_id" in res, res

        deleted = v5_session.run_decay(ep, decay_hours=72)
        assert ep.get_episode(eid) is not None, "pinned episode wrongly decayed"
        assert eid not in [d.get("invocation_num") for d in deleted]
        # authored memories never decay (separate table, untouched by run_decay)
        assert len(ep.all_authored_memories()) == 1, "authored memory lost"
        # two embeds: the pinned episode summary + the authored memory
        assert len(sem.added) == 2, sem.added
    return "pin + author in one call; both survive decay"


def scenario_substantive_window_filters_idle():
    with tempfile.TemporaryDirectory() as td:
        ep = EpisodicStore(str(Path(td) / "ep.db"))
        # Interleave idle (yield-only, no journal/internal) with substantive turns.
        for i in range(20):
            _episode(ep, invocation_num=1, focus=f"idle {i}", actions=["pause_turn"])
        s_journal = _episode(ep, invocation_num=1, focus="wrote",
                             journal="I drafted an essay.", actions=["pause_turn"])
        s_action = _episode(ep, invocation_num=1, focus="searched",
                            actions=["web_search", "pause_turn"])
        s_reflect = _episode(ep, invocation_num=1, focus="thought",
                             internal="quiet, content", actions=["pause_turn"])
        for i in range(20, 25):
            _episode(ep, invocation_num=1, focus=f"idle {i}", actions=["pause_turn"])

        subs = ep.recent_substantive_episodes(n=10)
        ids = [e["id"] for e in subs]
        # Only the three signal-bearing turns qualify (journal / action / reflective).
        assert ids == [s_journal, s_action, s_reflect], ids
        # Pure-idle yield-only turns are excluded entirely.
        assert all(e.get("journal_entry") or e.get("internal_state")
                   or any(a != "pause_turn" for a in e["actions_taken"]) for e in subs), subs
    return "recent_substantive_episodes keeps journal/action/reflective, drops idle yield-only"


SCENARIOS = [
    ("1: tools omit notes-to-self", scenario_tools_omit_notes_to_self),
    ("2: context has no AGENTS.md / not focus-seeded", scenario_context_no_agents_md_and_not_focus_seeded),
    ("3: authored memory written + embedded", scenario_authored_memory_written_and_embedded),
    ("4: pin + author survive decay", scenario_combined_pin_and_author_survive_decay),
    ("5: substantive window filters idle turns", scenario_substantive_window_filters_idle),
]


def test_scenario_1(): scenario_tools_omit_notes_to_self()
def test_scenario_2(): scenario_context_no_agents_md_and_not_focus_seeded()
def test_scenario_3(): scenario_authored_memory_written_and_embedded()
def test_scenario_4(): scenario_combined_pin_and_author_survive_decay()
def test_scenario_5(): scenario_substantive_window_filters_idle()


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
