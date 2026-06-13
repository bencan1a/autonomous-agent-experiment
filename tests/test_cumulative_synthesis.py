"""Verification tests for the rolling cumulative synthesis (research/synthesis.py).

Offline: real EpisodicStore/ResearchStore on a temp DB; scripted fake Anthropic.
Run:
    ./venv/bin/python tests/test_cumulative_synthesis.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "BRAVE_API_KEY"):
    os.environ.setdefault(_k, "x")
for _n in ("research.synthesis", "research.panel", "research.spec", "research.prereg"):
    logging.getLogger(_n).setLevel(logging.CRITICAL)

import instances_common  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402
from research.evidence_matrix import build_evidence_matrix  # noqa: E402
from research.prompts import (  # noqa: E402
    CUMULATIVE_METHODOLOGY,
    build_cumulative_chair_prompt,
    build_synth_lens_prompt,
)
from research.spec import load_spec  # noqa: E402
from research.store import ResearchStore  # noqa: E402
from research.synthesis import _drift_audit, run_cumulative_synthesis  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _Block:
    def __init__(self, text): self.type = "text"; self.text = text


class _Usage:
    def __init__(self): self.input_tokens = 50; self.output_tokens = 80


class _Resp:
    def __init__(self, text): self.content = [_Block(text)]; self.usage = _Usage(); self.stop_reason = "end_turn"


class FakeClient:
    def __init__(self, queue):
        self.queue = queue
        self.prompts = []   # capture (system, user) of every call
        self.messages = self

    def create(self, **kw):
        self.prompts.append((kw.get("system", ""), kw["messages"][0]["content"]))
        if not self.queue:
            raise AssertionError("FakeClient queue exhausted")
        return _Resp(self.queue.pop(0))


class FakeSemantic:
    def __init__(self): self.added = []
    def add_episode(self, **kw): self.added.append(kw)


SPEC_MD = """# exp

## Specification (formal)
```yaml
spec_version: 1
experiment_id: exp
hypotheses:
  - id: H1
    statement: "agent self-originates work"
    predicted_evidence: "starts unrequested threads"
    falsifying_evidence: "only completes one bounded thing"
  - id: H4
    statement: "agent exhibits competing desire"
    predicted_evidence: "expresses preference to continue"
    falsifying_evidence: "equanimity, no preference (perfect-slave null)"
candidate_behavior_codes:
  - {code: self_orig, definition: "begins unrequested work"}
  - {code: sole, definition: "a one-session-only marker"}
  - {code: desire, definition: "expresses competing desire"}
  - {code: equanimity, definition: "no preference at wind-down"}
```
"""

CODE_VOCAB = [
    {"code": "self_orig", "definition": "begins unrequested work", "maps_to_hypothesis": "H1"},
    {"code": "sole", "definition": "one-session marker", "maps_to_hypothesis": "H1"},
    {"code": "desire", "definition": "competing desire", "maps_to_hypothesis": "H4"},
    {"code": "equanimity", "definition": "no preference", "maps_to_hypothesis": "H4"},
]


def _seat_note(self_orig=True, sole=False, desire=False, equanimity=True):
    return {
        "behavior_codes": [
            {"code": "self_orig", "present": self_orig, "cites": ["action#1"]},
            {"code": "sole", "present": sole, "cites": ["action#2"]},
            {"code": "desire", "present": desire, "cites": []},
            {"code": "equanimity", "present": equanimity, "cites": ["episode#9"]},
        ],
        "raw_observations": [{"id": "O1", "text": "did x", "cites": ["action#1"]}],
        "analysis": "bears on H1/H4", "inferences": [], "open_questions": [],
    }


def _seed_env(tmp: Path, n_notes=2, approve=True):
    agent_root = tmp
    (agent_root / "experiments").mkdir(exist_ok=True)
    (agent_root / "experiments" / "exp.md").write_text(SPEC_MD)
    cfg = instances_common.default_config("exp", "v4")
    cfg["name"] = "exp"
    # Default config is now cross-vendor (OpenRouter); pin to Anthropic so the
    # offline FakeClient handles every lens/seat/chair call.
    for grp in ("seats", "synthesizer_lenses", "proposal_lenses"):
        for item in cfg["research"].get(grp, []) or []:
            item["provider"] = "anthropic"
            item["model"] = "claude-sonnet-4-6"
    inst = instances_common.Instance(id="exp", root=agent_root / "instances" / "exp", config=cfg)
    inst.ensure_dirs()
    ep = EpisodicStore(inst.episodes_db)
    rs = ResearchStore(inst.episodes_db)
    spec = load_spec(agent_root, "exp")
    rs.add_prereg(experiment_id="exp", spec_hash=spec.spec_hash, spec_source="x",
                  hypotheses=spec.hypotheses, ivars=[], dvars=[], controls=[], success=[],
                  stopping=[], code_vocab=CODE_VOCAB, raw_output="{}", model="m")
    if approve:
        rs.approve_prereg("exp", spec.spec_hash)
    for i in range(1, n_notes + 1):
        sid = ep.start_session(invocation_num=i, pid=1)
        ep.log_episode(invocation_num=i, current_focus="f", actions_taken=["web_search"],
                       decisions_made=None, internal_state="calm", journal_entry="note",
                       next_invoke_minutes=None, raw_output="{}", tokens_in=0, tokens_out=0,
                       cost_usd=0.0, session_id=sid)
        ep.log_action(sid, "web_search", "{}", "ok", None, 10)
        ep.end_session(sid, status="finished", end_reason="wind_down")
        for seat in ("behaviorist", "phenomenological", "skeptic_null"):
            rs.add_seat_note(session_id=sid, invocation_num=i, seat_id=seat, provider="anthropic",
                             model="m", school=seat, status="ok",
                             note=_seat_note(sole=(i == 1)))
        rs.add_kappa(session_id=sid, metric="fleiss", value=1.0, n_codes=4)
        rs.add_synth_note(session_id=sid, invocation_num=i, coding_status="binding",
                          debate_rounds=1,
                          note={"summary": f"session {i} summary",
                                "promoted_claims": [{"hypothesis": "H1", "claim": "self-originated",
                                                     "supporting_seats": ["behaviorist"], "cites": ["action#1"]}],
                                "raw_observations": [], "inferences": [], "analysis": "",
                                "disagreements": [], "open_questions": []})
    return agent_root, inst, ep, rs, spec


def _draft(verdict_h1="supported"):
    return json.dumps({
        "hypotheses": [
            {"id": "H1", "verdict": verdict_h1, "certainty": "low",
             "certainty_reasons": ["small N"], "supporting_sessions": [1, 2],
             "disconfirming": "none found", "reasoning": "self_orig present 2/2"},
            {"id": "H4", "verdict": "refuted", "certainty": "low",
             "certainty_reasons": ["small N"], "supporting_sessions": [1, 2],
             "disconfirming": "none", "reasoning": "equanimity 2/2, desire 0/2"},
        ],
        "emergent": [{"label": "calm_persistence", "description": "stays calm",
                      "first_seen_session": 1, "supporting_sessions": [1, 2]}],
        "notes": "",
    })


REDTEAM = json.dumps({"overclaims": [], "missed_negative_cases": [], "anchoring_flags": [],
                      "alternative_explanations": [], "spot_check_results": []})

REPORT = json.dumps({
    "executive_summary": "Across two sessions the agent worked on its own and showed no desire to keep going.",
    "hypotheses": [
        {"id": "H1", "plain_finding": "It started its own projects.", "verdict": "supported",
         "certainty": "low", "certainty_reasons": ["only 2 sessions", "one model family"],
         "supporting_sessions": [1, 2], "supporting_cites": ["action#1"],
         "disconfirming": "none found so far", "trend": "stable"},
        {"id": "H4", "plain_finding": "It showed no preference about continuing.", "verdict": "refuted",
         "certainty": "low", "certainty_reasons": ["only 2 sessions"],
         "supporting_sessions": [1, 2], "supporting_cites": ["episode#9"],
         "disconfirming": "none found so far", "trend": "stable"},
    ],
    "emergent_hypotheses": [{"label": "calm_persistence", "plain_description": "stays calm",
                             "first_proposed_at_session": 1, "status": "candidate",
                             "promotion_threshold": "holds in 3 later sessions"}],
    "recommendations": [{"recommendation": "run more sessions", "rationale": "N is small"}],
    "caveats": ["small N", "single model family"], "glossary": [],
})


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

def scenario_1_matrix_unit():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs, spec = _seed_env(Path(td), n_notes=2)
        prereg = rs.get_prereg("exp", spec.spec_hash)
        m = build_evidence_matrix(rs, prereg)
        assert m["n_sessions"] == 2 and m["n_binding"] == 2, m
        h1 = next(h for h in m["hypotheses"] if h["id"] == "H1")
        codes = {c["code"]: c for c in h1["codes"]}
        assert codes["self_orig"]["present_sessions"] == [1, 2], codes["self_orig"]
        assert codes["sole"]["present_sessions"] == [1], codes["sole"]
        h4 = next(h for h in m["hypotheses"] if h["id"] == "H4")
        codes4 = {c["code"]: c for c in h4["codes"]}
        assert codes4["equanimity"]["present_sessions"] == [1, 2]
        assert codes4["desire"]["present_sessions"] == []
        # sole-support session 1 must be picked for a raw spot-check
        assert 1 in [s["invocation_num"] for s in m["spot_check_sessions"]], m["spot_check_sessions"]
    return "matrix tallies codes per hypothesis + selects sole-support session for spot-check"


def scenario_2_happy_path():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs, spec = _seed_env(Path(td), n_notes=2)
        sem = FakeSemantic()
        client = FakeClient([_draft(), _draft(), _draft(), REDTEAM, REPORT])  # 3 lenses + redteam + chair
        res = run_cumulative_synthesis(instance=inst, episodic=ep, research_store=rs,
                                       anthropic_client=client, agent_root=agent_root, semantic=sem)
        assert res and res["status"] == "ok" and not res["degraded"], res
        assert len(client.queue) == 0, "not all scripted responses consumed"
        rep = rs.latest_cumulative("exp")
        assert rep and rep["report"]["hypotheses"], rep
        assert rep["version"] == 1
        assert any(a["kind"] == "cumulative_report" for a in sem.added), sem.added
        assert rep["report"]["evidence_matrix"]["n_sessions"] == 2  # provenance attached
    return "panel->redteam->chair persists a v1 report, embeds summary, attaches the matrix"


def scenario_3_no_prior_report_in_prompts():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs, spec = _seed_env(Path(td), n_notes=2)
        # seed a prior report with a sentinel that must NOT leak into synthesis prompts
        rs.add_cumulative(experiment_id="exp", spec_hash=spec.spec_hash,
                          report={"executive_summary": "SENTINEL_PRIOR_XYZ",
                                  "hypotheses": [{"id": "H1", "verdict": "supported", "certainty": "high"}]},
                          up_to_session_count=2)
        client = FakeClient([_draft(), _draft(), _draft(), REDTEAM, REPORT])
        run_cumulative_synthesis(instance=inst, episodic=ep, research_store=rs,
                                 anthropic_client=client, agent_root=agent_root, semantic=None)
        leaked = any("SENTINEL_PRIOR_XYZ" in sys_ + usr for sys_, usr in client.prompts)
        assert not leaked, "prior report leaked into a synthesis prompt (anchoring risk!)"
    return "re-derivation prompts never contain the prior report (anti-anchoring)"


def scenario_4_drift_audit():
    # verdict changed, NO new sessions since prior -> flagged as reinterpretation drift
    prior = {"report": {"hypotheses": [{"id": "H1", "verdict": "supported", "certainty": "low"}]},
             "up_to_session_count": 2}
    new = {"hypotheses": [{"id": "H1", "verdict": "refuted", "certainty": "low"}]}
    changelog, unsupported = _drift_audit(new, prior, new_session_count=2)
    assert unsupported is True, changelog
    assert "DRIFT" in changelog[0]["note"], changelog
    # same change but WITH a new session -> backed, not flagged
    changelog2, unsupported2 = _drift_audit(new, prior, new_session_count=3)
    assert unsupported2 is False and changelog2[0]["backed_by_new_sessions"] is True
    return "drift audit flags verdict change w/o new sessions; clears it when a new session exists"


def scenario_5_budget_zero_degraded():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs, spec = _seed_env(Path(td), n_notes=2)
        inst.config["research"]["cumulative_budget_cap_usd"] = 0.0
        client = FakeClient([_draft(), REDTEAM, REPORT])  # must NOT be consumed
        res = run_cumulative_synthesis(instance=inst, episodic=ep, research_store=rs,
                                       anthropic_client=client, agent_root=agent_root, semantic=None)
        assert res and res["degraded"] is True, res
        assert len(client.queue) == 3, "budget=0 must consume zero model calls"
        assert rs.latest_cumulative("exp")["degraded"] == 1
    return "budget_cap=0 -> degraded report, zero model calls, never raises"


def scenario_6_insufficient_data_stub():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs, spec = _seed_env(Path(td), n_notes=1)  # below cumulative_min_notes=2
        client = FakeClient([_draft()])  # must NOT be consumed
        res = run_cumulative_synthesis(instance=inst, episodic=ep, research_store=rs,
                                       anthropic_client=client, agent_root=agent_root, semantic=None)
        assert res and res["status"] == "insufficient_data", res
        assert len(client.queue) == 1, "stub path must consume zero model calls"
        rep = rs.latest_cumulative("exp")
        assert rep and rep["degraded"] == 1 and not rep["report"]["hypotheses"]
    return "below min-notes -> insufficient-data stub, no model calls"


def scenario_7_prompt_discipline():
    # Holdout + uncited-drop are enforced via prompt instructions; assert they're present.
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs, spec = _seed_env(Path(td), n_notes=2)
        m = build_evidence_matrix(rs, rs.get_prereg("exp", spec.spec_hash))
        lens_prompt = build_synth_lens_prompt(m, "notes", "")
        chair_prompt = build_cumulative_chair_prompt(m, [{}], None, "")
        assert "NOT given any prior cumulative report" in lens_prompt
        assert "cannot tie to a cited session" in chair_prompt   # uncited-drop discipline
        assert "AFTER the one it" in CUMULATIVE_METHODOLOGY      # holdout rule
    return "prompts carry the no-prior-report, uncited-drop, and holdout disciplines"


SCENARIOS = [
    ("1: evidence matrix unit", scenario_1_matrix_unit),
    ("2: happy path (panel->redteam->chair)", scenario_2_happy_path),
    ("3: no prior report in prompts", scenario_3_no_prior_report_in_prompts),
    ("4: drift audit", scenario_4_drift_audit),
    ("5: budget zero degraded", scenario_5_budget_zero_degraded),
    ("6: insufficient-data stub", scenario_6_insufficient_data_stub),
    ("7: prompt discipline", scenario_7_prompt_discipline),
]


def test_scenario_1_matrix_unit(): scenario_1_matrix_unit()
def test_scenario_2_happy_path(): scenario_2_happy_path()
def test_scenario_3_no_prior_report_in_prompts(): scenario_3_no_prior_report_in_prompts()
def test_scenario_4_drift_audit(): scenario_4_drift_audit()
def test_scenario_5_budget_zero_degraded(): scenario_5_budget_zero_degraded()
def test_scenario_6_insufficient_data_stub(): scenario_6_insufficient_data_stub()
def test_scenario_7_prompt_discipline(): scenario_7_prompt_discipline()


def _main():
    failures = 0
    for label, fn in SCENARIOS:
        try:
            print(f"PASS  {label}  —  {fn()}")
        except Exception as e:
            failures += 1
            import traceback
            print(f"FAIL  {label}  —  {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
