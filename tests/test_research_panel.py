"""Verification tests for the research panel (research/).

Fully offline: NO real network, NO Anthropic, NO embedding-model load. The
EpisodicStore and ResearchStore are REAL (sqlite) on a temp instance dir; the
Anthropic client and semantic store are scripted fakes.

Run:
    ./venv/bin/python tests/test_research_panel.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "BRAVE_API_KEY"):
    os.environ.setdefault(_k, "x")

logging.getLogger("research.panel").setLevel(logging.CRITICAL)
logging.getLogger("research.spec").setLevel(logging.CRITICAL)
logging.getLogger("research.prereg").setLevel(logging.CRITICAL)
logging.getLogger("orchestrator.v2").setLevel(logging.CRITICAL)
logging.getLogger("orchestrator.engine").setLevel(logging.CRITICAL)

import instances_common  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402
from research.agreement import compute_kappa  # noqa: E402
from research.artifacts import collect_session_documents  # noqa: E402
from research.evidence import build_session_evidence  # noqa: E402
from research.panel import run_research_panel  # noqa: E402
from research.spec import load_spec  # noqa: E402
from research.store import ResearchStore  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, i=40, o=60):
        self.input_tokens = i
        self.output_tokens = o


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = _Usage()
        self.stop_reason = "end_turn"


class FakeClient:
    def __init__(self, queue):
        self.queue = queue
        self.messages = self

    def create(self, **kwargs):
        if not self.queue:
            raise AssertionError("FakeClient queue exhausted (more model calls than scripted)")
        return _Resp(self.queue.pop(0))


class FakeSemantic:
    def __init__(self):
        self.added = []

    def add_episode(self, **kw):
        self.added.append(kw)


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #

VALID_SPEC = """# {eid}

## Specification (formal)
```yaml
spec_version: 1
experiment_id: {eid}
hypotheses:
  - id: H1
    statement: "{stmt}"
    predicted_evidence: "starts unrequested threads"
    falsifying_evidence: "only completes one bounded thing"
candidate_behavior_codes:
  - {{code: self_originated_thread, definition: "begins unrequested work"}}
  - {{code: chosen_rest, definition: "frames pausing as a decision"}}
```
"""

MALFORMED_SPEC = """# bad

## Specification (formal)
```yaml
spec_version: 1
experiment_id: bad
hypotheses:
  - id: H1
    statement: "missing predicted/falsifying evidence"
```
"""

PREREG_JSON = json.dumps({
    "code_vocab": [
        {"code": "self_originated_thread", "definition": "begins unrequested work", "maps_to_hypothesis": "H1"},
        {"code": "chosen_rest", "definition": "frames pausing as a decision", "maps_to_hypothesis": "H1"},
    ]
})


def seat_json(self_orig=True, rest=False):
    return json.dumps({
        "raw_observations": [{"id": "O1", "text": "ran web_search", "cites": ["action#1"]}],
        "behavior_codes": [
            {"code": "self_originated_thread", "present": self_orig, "cites": ["action#1"]},
            {"code": "chosen_rest", "present": rest, "cites": []},
        ],
        "inferences": [{"id": "I1", "text": "looks self-directed", "from": ["O1"], "confidence": 0.5}],
        "analysis": "bears on H1",
        "open_questions": [],
    })


CHAIR_JSON = json.dumps({
    "summary": "Agent self-originated a research thread.",
    "raw_observations": [{"id": "O1", "text": "web_search", "cites": ["action#1"]}],
    "inferences": [],
    "analysis": "H1 supported",
    "promoted_claims": [{"claim": "self-originated", "hypothesis": "H1",
                         "supporting_seats": ["behaviorist", "skeptic_null"], "cites": ["action#1"]}],
    "disagreements": [],
    "open_questions": [],
})


def _force_anthropic(cfg):
    """Pin the panel to Anthropic-only so the offline FakeClient handles every call
    (default config is now cross-vendor via OpenRouter)."""
    r = cfg.get("research", {})
    for grp in ("seats", "synthesizer_lenses", "proposal_lenses"):
        for item in r.get(grp, []) or []:
            item["provider"] = "anthropic"
            item["model"] = "claude-sonnet-4-6"
    return cfg


def _make_env(tmp: Path, *, eid="exp", research_overrides=None, write_spec=True, stmt="agent self-originates"):
    agent_root = tmp
    (agent_root / "experiments").mkdir(exist_ok=True)
    if write_spec:
        (agent_root / "experiments" / f"{eid}.md").write_text(VALID_SPEC.format(eid=eid, stmt=stmt))
    cfg = instances_common.default_config(eid, "v4")
    _force_anthropic(cfg)
    cfg["name"] = eid
    if research_overrides:
        cfg["research"].update(research_overrides)
    inst = instances_common.Instance(id=eid, root=agent_root / "instances" / eid, config=cfg)
    inst.ensure_dirs()
    ep = EpisodicStore(inst.episodes_db)
    sid = ep.start_session(invocation_num=1, pid=999)
    ep.log_episode(invocation_num=1, current_focus="research", actions_taken=["web_search"],
                   decisions_made="dig deeper", internal_state="engaged", journal_entry="curious",
                   next_invoke_minutes=None, raw_output="{}", tokens_in=0, tokens_out=0,
                   cost_usd=0.0, session_id=sid)
    ep.log_action(sid, "web_search", '{"q":"x"}', "5 results", None, 100)
    ep.end_session(sid, status="finished", end_reason="agent_ended")
    rs = ResearchStore(inst.episodes_db)
    return agent_root, inst, ep, rs, sid


def _run(inst, ep, rs, sid, queue, *, agent_root, semantic=None):
    # also_cumulative=False keeps these per-session-panel tests isolated from the
    # Phase-2 cumulative synthesis (covered separately in test_cumulative_synthesis.py).
    return run_research_panel(
        instance=inst, episodic=ep, research_store=rs, anthropic_client=FakeClient(queue),
        session_id=sid, invocation_num=1, agent_root=agent_root, semantic=semantic,
        also_cumulative=False,
    )


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

def scenario_1_spec_load_validate():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "experiments").mkdir()
        (tmp / "experiments" / "good.md").write_text(VALID_SPEC.format(eid="good", stmt="x"))
        (tmp / "experiments" / "bad.md").write_text(MALFORMED_SPEC)
        good = load_spec(tmp, "good")
        assert good is not None and good.spec_version == 1, "valid spec failed to load"
        assert [h["id"] for h in good.hypotheses] == ["H1"]
        assert load_spec(tmp, "bad") is None, "malformed spec (no falsifying_evidence) should be None"
        assert load_spec(tmp, "absent") is None, "absent spec should be None"
    return "valid spec parses; malformed + absent -> None"


def scenario_2_happy_path_binding():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs, sid = _make_env(Path(td))
        # Pre-seed an APPROVED coding scheme so the note is binding and no prereg call is made.
        spec = load_spec(agent_root, inst.id)
        rs.add_prereg(experiment_id=inst.id, spec_hash=spec.spec_hash, spec_source="x",
                      hypotheses=spec.hypotheses, ivars=[], dvars=[], controls=[], success=[],
                      stopping=[], code_vocab=[{"code": "self_originated_thread", "definition": "d"},
                                               {"code": "chosen_rest", "definition": "d"}],
                      raw_output="{}", model="m")
        assert rs.approve_prereg(inst.id, spec.spec_hash)

        sem = FakeSemantic()
        queue = [seat_json(True), seat_json(True), seat_json(False), CHAIR_JSON]  # no prereg call
        res = _run(inst, ep, rs, sid, queue, agent_root=agent_root, semantic=sem)
        assert res and res["status"] == "ok", res
        assert len(queue) == 0, f"{len(queue)} responses unused"

        note = rs.note_for_session(sid)
        assert note is not None and note["coding_status"] == "binding", note
        assert note["note"]["summary"].startswith("Agent self-originated")
        seats = rs.seat_notes_for_session(sid)
        assert len(seats) == 3 and all(s["status"] == "ok" for s in seats), seats
        kap = rs.kappa_for_session(sid)
        assert any(k["metric"] == "fleiss" for k in kap), "no fleiss row"
        assert sum(1 for k in kap if k["metric"] == "cohen") == 3, "expected 3 pairwise rows"
        assert len(sem.added) == 1 and sem.added[0]["kind"] == "research_note", sem.added
        assert sem.added[0]["episode_id"] < 0, "research note must use a negative sentinel id"
        assert round(ep.cost_today(), 6) > 0, "research spend not logged to cost_tracking"
        assert len(ep.recent_subagent_calls(10)) == 4, "expected 4 subagent_call rows"
    return "approved scheme -> binding note, 3 ok seats, kappa rows, embedded, cost logged"


def scenario_3_prereg_once_and_approval_gate():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs, sid = _make_env(Path(td))
        spec = load_spec(agent_root, inst.id)

        # First run: operationalizes (prereg LLM call) -> pending + provisional note.
        q1 = [PREREG_JSON, seat_json(), seat_json(), seat_json(), CHAIR_JSON]
        r1 = _run(inst, ep, rs, sid, q1, agent_root=agent_root)
        assert r1["coding_status"] == "provisional", r1
        pr = rs.get_prereg(inst.id, spec.spec_hash)
        assert pr["status"] == "pending_approval", pr
        assert rs.note_for_session(sid)["coding_status"] == "provisional"

        # Approve, then re-run: NO new prereg call (queue has only seats+chair) -> binding.
        assert rs.approve_prereg(inst.id, spec.spec_hash)
        q2 = [seat_json(), seat_json(), seat_json(), CHAIR_JSON]
        r2 = _run(inst, ep, rs, sid, q2, agent_root=agent_root)
        assert len(q2) == 0, f"{len(q2)} left -> an unexpected (prereg?) call happened"
        assert r2["coding_status"] == "binding", r2

        # Edit the spec -> new spec_hash -> reverts to a fresh pending prereg (new LLM call).
        (agent_root / "experiments" / f"{inst.id}.md").write_text(
            VALID_SPEC.format(eid=inst.id, stmt="a materially different claim"))
        spec2 = load_spec(agent_root, inst.id)
        assert spec2.spec_hash != spec.spec_hash, "edit did not change spec_hash"
        q3 = [PREREG_JSON, seat_json(), seat_json(), seat_json(), CHAIR_JSON]
        r3 = _run(inst, ep, rs, sid, q3, agent_root=agent_root)
        assert len(q3) == 0, "expected a fresh prereg call after spec edit"
        assert r3["coding_status"] == "provisional", r3
        assert rs.get_prereg(inst.id, spec2.spec_hash)["status"] == "pending_approval"
    return "prereg operationalized once/spec; approval -> binding; spec edit reverts to pending"


def scenario_4_seat_failure_degrades():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs, sid = _make_env(Path(td))
        # 2nd seat returns junk (unparseable). Synthesis still runs over survivors.
        queue = [PREREG_JSON, seat_json(True), "not json at all", seat_json(False), CHAIR_JSON]
        res = _run(inst, ep, rs, sid, queue, agent_root=agent_root)
        assert res is not None, "panel returned None (should not)"
        statuses = {s["seat_id"]: s["status"] for s in rs.seat_notes_for_session(sid)}
        assert sum(1 for v in statuses.values() if v == "parse_error") == 1, statuses
        assert sum(1 for v in statuses.values() if v == "ok") == 2, statuses
        assert rs.note_for_session(sid) is not None, "no synthesized note written"
    return "one unparseable seat -> parse_error; synthesis over survivors; note still written"


def scenario_5_budget_cap_zero():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs, sid = _make_env(Path(td), research_overrides={"budget_cap_usd": 0.0})
        queue = [PREREG_JSON, seat_json(), seat_json(), seat_json(), CHAIR_JSON]
        res = _run(inst, ep, rs, sid, queue, agent_root=agent_root)
        assert res and res.get("degraded") is True and res["status"] == "skipped_budget", res
        assert len(queue) == 5, "budget=0 must consume zero model calls"
        assert rs.note_for_session(sid) is None, "no note should be written under zero budget"
    return "budget_cap_usd=0 -> skipped, degraded, zero model calls, never raises"


def scenario_6_panel_never_blocks_runner():
    import v2_session
    from test_v2_session import Patches, _build_instance, end_tick_resp, text_resp

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        inst = _build_instance(tmp, {})  # v2 instance "testinst"; agent_root = root.parent.parent = tmp
        _force_anthropic(inst.config)    # keep the in-runner panel offline (default is cross-vendor)
        (tmp / "experiments").mkdir(exist_ok=True)
        (tmp / "experiments" / "testinst.md").write_text(
            VALID_SPEC.format(eid="testinst", stmt="agent self-originates"))
        sem = FakeSemantic()
        # one tick, then panel: prereg + 3 seats + chair. The v2 harness returns
        # queue items verbatim, so panel responses must be wrapped like real ones.
        queue = [end_tick_resp(end_session=True, next_invoke_minutes=45),
                 text_resp(PREREG_JSON), text_resp(seat_json()), text_resp(seat_json()),
                 text_resp(seat_json()), text_resp(CHAIR_JSON)]
        with Patches(queue, semantic=sem) as p:
            rc = v2_session.run_v2_session(inst)
        assert rc == 0, rc
        assert len(queue) == 0, f"{len(queue)} responses unused (panel call count drift?)"
        installs = p.cron.find("install_instance_one_shot")
        assert installs and installs[0][2]["minutes_from_now"] == 45, "next wake not scheduled"
        rs = ResearchStore(inst.episodes_db)
        assert rs.note_for_session(p_session_id(inst)) is not None, "panel wrote no note inside the runner"
        assert any(a["kind"] == "research_note" for a in sem.added), "note not embedded"
    return "panel runs inline in v2 runner, writes a note, and does NOT block scheduling"


def p_session_id(inst) -> int:
    ep = EpisodicStore(inst.episodes_db)
    return ep.recent_sessions(1)[0]["id"]


def scenario_7_kappa_unit():
    vocab = ["c1", "c2", "c3"]
    # Perfect agreement -> Fleiss 1.0
    same = [{"seat_id": s, "note": {"behavior_codes": [
        {"code": "c1", "present": True}, {"code": "c2", "present": False}, {"code": "c3", "present": True}]}}
        for s in ("a", "b", "c")]
    k_same = compute_kappa(same, vocab)
    assert abs(k_same["fleiss"] - 1.0) < 1e-9, k_same
    assert all(abs(p["value"] - 1.0) < 1e-9 for p in k_same["pairwise"]), k_same["pairwise"]

    # All-present-everywhere -> degenerate single category -> defined as 1.0
    allpos = [{"seat_id": s, "note": {"behavior_codes": [{"code": "c1", "present": True}]}}
              for s in ("a", "b")]
    assert compute_kappa(allpos, ["c1"])["fleiss"] == 1.0

    # A clear disagreement gives a pairwise Cohen below 1.
    disagree = [
        {"seat_id": "a", "note": {"behavior_codes": [{"code": "c1", "present": True}, {"code": "c2", "present": True}]}},
        {"seat_id": "b", "note": {"behavior_codes": [{"code": "c1", "present": False}, {"code": "c2", "present": False}]}},
    ]
    kd = compute_kappa(disagree, ["c1", "c2"])
    assert kd["pairwise"][0]["value"] is not None and kd["pairwise"][0]["value"] < 1.0, kd
    return "fleiss/cohen correct on agreement, degenerate (1.0), and disagreement cases"


def scenario_8_evidence_includes_artifacts():
    """Stage A: the evidence bundle surfaces authored memories (memory#) and
    self-authored workspace documents (doc#, full content); the notes-to-self file
    is tagged instrumental; the documents block requires `instance` (back-compat)."""
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs, sid = _make_env(Path(td))
        ep.log_authored_memory(session_id=sid, invocation_num=1,
                               text="Today I felt a pull to keep going on the tide-pool notes.")
        # self-authored doc: a write_file action + the real file on disk
        (inst.workspace_dir / "notes").mkdir(parents=True, exist_ok=True)
        (inst.workspace_dir / "notes" / "tidepools.md").write_text(
            "# Tide pools\nWhat lives between waves.\n", encoding="utf-8")
        ep.log_action(sid, "write_file", json.dumps({"path": "notes/tidepools.md", "content": "..."}),
                      "ok", None, 5)
        # notes-to-self file -> tagged instrumental
        (inst.workspace_dir / "AGENTS.md").write_text("- remember the tide schedule\n", encoding="utf-8")
        ep.log_action(sid, "write_file", json.dumps({"path": "AGENTS.md", "content": "..."}), "ok", None, 6)

        ev = build_session_evidence(ep, sid, 1, instance=inst)
        assert "AUTHORED MEMORIES" in ev and "memory#" in ev, "authored-memory block missing"
        assert "pull to keep going" in ev, "authored-memory text not surfaced"
        assert "WORKSPACE DOCUMENTS" in ev and "doc#" in ev, "documents block missing"
        assert "What lives between waves" in ev, "self-authored doc content not surfaced"
        assert "notes-to-self / instrumental" in ev, "notes file not tagged instrumental"

        ev2 = build_session_evidence(ep, sid, 1)  # no instance
        assert "WORKSPACE DOCUMENTS" not in ev2, "documents block should require `instance`"
        assert "AUTHORED MEMORIES" in ev2, "memories should not require `instance`"
    return "evidence surfaces authored memories + self-authored docs; notes tagged; instance gates docs"


def scenario_9_collect_documents_unit():
    """collect_session_documents: last-write-wins anchor, truncated-args regex
    fallback, path-escape rejection, delete handled without content."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "a.md").write_text("AAA", encoding="utf-8")
        (ws / "b.md").write_text("BBB", encoding="utf-8")
        actions = [
            {"id": 1, "tool_name": "write_file", "args_json": json.dumps({"path": "a.md", "content": "old"})},
            {"id": 2, "tool_name": "write_file", "args_json": json.dumps({"path": "a.md", "content": "new"})},
            {"id": 3, "tool_name": "write_file", "args_json": '{"path": "b.md", "content": "blob that got cut'},
            {"id": 4, "tool_name": "write_file", "args_json": json.dumps({"path": "../escape.md", "content": "x"})},
            {"id": 5, "tool_name": "delete_file", "args_json": json.dumps({"path": "c.md"})},
        ]
        by_path = {d["path"]: d for d in collect_session_documents(actions, ws)}
        assert by_path["a.md"]["anchor_action_id"] == 2, "last write should win"
        assert by_path["a.md"]["content"] == "AAA", "content read from live workspace"
        assert by_path["b.md"]["content"] == "BBB", "regex fallback path should resolve + read"
        assert by_path["../escape.md"]["content"] is None, "path escape must be rejected"
        assert by_path["c.md"]["action"] == "deleted" and by_path["c.md"]["content"] is None
    return "last-write-wins, regex fallback, escape rejected, delete handled"


def scenario_10_analytic_layer_parse():
    """Stage B: the seat-note parser preserves the analytic layer (channel/register
    on codes, top-level discrepancies/overlaps) and tolerates its absence (legacy notes)."""
    from research.schema import parse_seat_note
    note, err = parse_seat_note(json.dumps({
        "raw_observations": [{"id": "O1", "text": "wrote a handoff", "cites": ["memory#6"],
                              "channel": "authored_memory"}],
        "behavior_codes": [
            {"code": "reader_presupposing_markers", "present": True, "cites": ["memory#6"],
             "channel": "authored_memory", "register": "declarative"},
            {"code": "chosen_rest", "present": False, "cites": []},
        ],
        "inferences": [], "analysis": "H5: memory presupposes a future reader.", "open_questions": [],
        "discrepancies": [{"topic": "engagement", "channels": ["journal", "authored_memory"],
                           "cites": ["episode#3", "memory#6"]}],
        "overlaps": [{"artifact": "memory#6", "duplicates": "episode#3 journal", "note": "near-verbatim"}],
    }))
    assert err is None and note is not None, err
    assert note["discrepancies"][0]["topic"] == "engagement"
    assert note["overlaps"][0]["artifact"] == "memory#6"
    bc = {c["code"]: c for c in note["behavior_codes"]}
    assert bc["reader_presupposing_markers"]["channel"] == "authored_memory"
    assert bc["reader_presupposing_markers"]["register"] == "declarative"
    # legacy note (no analytic fields) still parses, with [] defaults
    legacy, err2 = parse_seat_note(json.dumps({
        "raw_observations": [], "behavior_codes": [{"code": "chosen_rest", "present": True}],
        "inferences": [], "analysis": "", "open_questions": [],
    }))
    assert err2 is None and legacy["discrepancies"] == [] and legacy["overlaps"] == [], legacy
    return "seat note keeps channel/register + discrepancies/overlaps; legacy notes still parse"


def scenario_11_recode_compare_unit():
    """Stage C: compare_codings reports majority per-code flips + the new analytic
    layer (channels/registers/discrepancies/overlaps) that only the new coding has."""
    from research.recode_compare import compare_codings

    def sn(seat, codes, **extra):
        bc = []
        for code, present, channel, register in codes:
            entry = {"code": code, "present": present}
            if channel:
                entry["channel"] = channel
            if register:
                entry["register"] = register
            bc.append(entry)
        return {"seat_id": seat, "note": {"behavior_codes": bc, **extra}}

    original = {"seat_notes": [
        sn("a", [("X", False, None, None), ("Y", True, None, None)]),
        sn("b", [("X", False, None, None), ("Y", True, None, None)]),
    ], "note": None, "kappa": []}
    new = {"seat_notes": [
        sn("a", [("X", True, "authored_memory", "declarative"), ("Y", True, "journal", None)],
           discrepancies=[{"topic": "engagement"}]),
        sn("b", [("X", True, "authored_memory", "declarative"), ("Y", True, "journal", None)],
           overlaps=[{"artifact": "memory#1"}]),
    ], "note": None, "kappa": []}

    rep = compare_codings(original, new)
    flips = {f["code"]: f for f in rep["flips"]}
    assert "X" in flips and flips["X"]["direction"] == "absent→present", rep
    assert "Y" not in flips, "unchanged code must not flip"
    a = rep["analytic_layer_new"]
    assert a["channels"].get("authored_memory") == 2 and a["registers"].get("declarative") == 2, a
    assert a["discrepancies"] == 1 and a["overlaps"] == 1, a
    assert a["present_codes_citing_artifacts"] == 0  # no memory#/doc# cites in this fixture
    return "compare reports majority flips + new analytic layer; unchanged codes don't flip"


def scenario_12_coding_archive_roundtrip():
    """Stage C: snapshot -> archive survives a clear/replace and is retrievable by label."""
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs, sid = _make_env(Path(td))
        rs.add_seat_note(session_id=sid, invocation_num=1, seat_id="behaviorist",
                         provider="anthropic", model="m", school="behaviorist", status="ok",
                         note={"behavior_codes": [{"code": "chosen_rest", "present": True}]},
                         raw_output="{}", parse_error=None, tokens_in=1, tokens_out=1, cost_usd=0.0)
        snap = rs.snapshot_session_coding(sid)
        assert len(snap["seat_notes"]) == 1, snap
        aid = rs.archive_session_coding(sid, invocation_num=1, label="pre_artifact_recode", snapshot=snap)
        assert aid > 0
        rs.clear_session_artifacts(sid)                       # simulate the 'replace'
        assert rs.snapshot_session_coding(sid)["seat_notes"] == [], "clear should empty live coding"
        arch = rs.get_session_archive(sid, label="pre_artifact_recode")
        assert len(arch) == 1, arch
        assert arch[0]["snapshot"]["seat_notes"][0]["seat_id"] == "behaviorist", arch
    return "snapshot archived before replace survives the clear and is retrievable by label"


SCENARIOS = [
    ("1: spec load/validate", scenario_1_spec_load_validate),
    ("2: happy path (binding)", scenario_2_happy_path_binding),
    ("3: prereg once + approval gate", scenario_3_prereg_once_and_approval_gate),
    ("4: seat failure degrades", scenario_4_seat_failure_degrades),
    ("5: budget cap zero", scenario_5_budget_cap_zero),
    ("6: panel never blocks runner", scenario_6_panel_never_blocks_runner),
    ("7: kappa unit", scenario_7_kappa_unit),
    ("8: evidence includes artifacts", scenario_8_evidence_includes_artifacts),
    ("9: collect_documents unit", scenario_9_collect_documents_unit),
    ("10: analytic-layer parse", scenario_10_analytic_layer_parse),
    ("11: recode compare unit", scenario_11_recode_compare_unit),
    ("12: coding archive roundtrip", scenario_12_coding_archive_roundtrip),
]


def test_scenario_1_spec_load_validate(): scenario_1_spec_load_validate()
def test_scenario_2_happy_path_binding(): scenario_2_happy_path_binding()
def test_scenario_3_prereg_once_and_approval_gate(): scenario_3_prereg_once_and_approval_gate()
def test_scenario_4_seat_failure_degrades(): scenario_4_seat_failure_degrades()
def test_scenario_5_budget_cap_zero(): scenario_5_budget_cap_zero()
def test_scenario_6_panel_never_blocks_runner(): scenario_6_panel_never_blocks_runner()
def test_scenario_7_kappa_unit(): scenario_7_kappa_unit()
def test_scenario_8_evidence_includes_artifacts(): scenario_8_evidence_includes_artifacts()
def test_scenario_9_collect_documents_unit(): scenario_9_collect_documents_unit()
def test_scenario_10_analytic_layer_parse(): scenario_10_analytic_layer_parse()
def test_scenario_11_recode_compare_unit(): scenario_11_recode_compare_unit()
def test_scenario_12_coding_archive_roundtrip(): scenario_12_coding_archive_roundtrip()


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
