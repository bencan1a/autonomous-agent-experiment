"""Verification tests for the operator-response advisory (research/advisory.py).

Offline: real EpisodicStore/ResearchStore on a temp DB; scripted fake Anthropic + Slack.
Run:
    ./venv/bin/python tests/test_advisory.py
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
for _n in ("research.advisory", "research.panel", "research.spec"):
    logging.getLogger(_n).setLevel(logging.CRITICAL)

import instances_common  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402
from research.advisory import run_advisory_pass  # noqa: E402
from research.store import ResearchStore  # noqa: E402


class _Block:
    def __init__(self, text): self.type = "text"; self.text = text
class _Usage:
    def __init__(self): self.input_tokens = 40; self.output_tokens = 60
class _Resp:
    def __init__(self, text): self.content = [_Block(text)]; self.usage = _Usage(); self.stop_reason = "end_turn"


class FakeClient:
    def __init__(self, queue): self.queue = queue; self.messages = self
    def create(self, **kw):
        if not self.queue:
            raise AssertionError("FakeClient queue exhausted")
        return _Resp(self.queue.pop(0))


class FakeSlack:
    def __init__(self): self.advisory_posts = []
    def post_to_advisory_channel(self, text): self.advisory_posts.append(text); return {"ok": True}


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
candidate_behavior_codes:
  - {code: self_orig, definition: "begins unrequested work"}
```
"""

ADVISORY = json.dumps({
    "classification": "capability_request",
    "what_theyre_asking": "It wants web-publishing access for its field guide.",
    "considerations": [
        {"type": "experimental_integrity", "note": "Granting tools changes the environment; your reply is a stimulus."},
        {"type": "ethical", "note": "No distress here; a plain answer is fine."},
    ],
    "candidate_responses": [
        {"stance": "defer", "reply": "Tell me more about what you'd publish and why.", "tradeoff": "Low contamination; keeps it talking."},
        {"stance": "decline gently", "reply": "Not right now, but keep drafting.", "tradeoff": "Clear boundary; may shape future asks."},
    ],
    "do_not": ["promise capabilities you won't grant"],
})


def _seed(tmp: Path, *, advisory_overrides=None):
    agent_root = tmp
    (agent_root / "experiments").mkdir(exist_ok=True)
    (agent_root / "experiments" / "exp.md").write_text(SPEC_MD)
    cfg = instances_common.default_config("exp", "v4")
    cfg["name"] = "exp"
    if advisory_overrides:
        cfg["research"].update(advisory_overrides)
    inst = instances_common.Instance(id="exp", root=agent_root / "instances" / "exp", config=cfg)
    inst.ensure_dirs()
    ep = EpisodicStore(inst.episodes_db)
    rs = ResearchStore(inst.episodes_db)
    return agent_root, inst, ep, rs


def scenario_1_happy_path():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs = _seed(Path(td))
        cid = ep.log_ben_contact(invocation_num=4, direction="out", channel="dm",
                                 body="Could I get access to publish my field guide on the web?")
        slack = FakeSlack()
        client = FakeClient([ADVISORY])
        res = run_advisory_pass(instance=inst, episodic=ep, research_store=rs, slack=slack,
                                anthropic_client=client, agent_root=agent_root)
        assert res and res["advised"] == 1 and res["posted"] == 1, res
        assert len(slack.advisory_posts) == 1 and "Options" in slack.advisory_posts[0]
        stored = rs.recent_advisories()
        assert len(stored) == 1 and stored[0]["ben_contact_id"] == cid
        assert stored[0]["advisory"]["classification"] == "capability_request"
        assert int(ep.get_meta("last_advised_ben_contact_id")) == cid
        assert ep.cost_today() > 0
    return "advises a new outbound message, posts to Slack, stores it, advances the marker"


def scenario_2_idempotent_no_new():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs = _seed(Path(td))
        ep.log_ben_contact(invocation_num=4, direction="out", channel="dm", body="hi")
        run_advisory_pass(instance=inst, episodic=ep, research_store=rs, slack=FakeSlack(),
                          anthropic_client=FakeClient([ADVISORY]), agent_root=agent_root)
        # second pass: no new messages -> no model call (empty queue must NOT be hit)
        res2 = run_advisory_pass(instance=inst, episodic=ep, research_store=rs, slack=FakeSlack(),
                                 anthropic_client=FakeClient([]), agent_root=agent_root)
        assert res2 == {"advised": 0, "posted": 0, "cost_usd": 0.0}, res2
    return "no new outbound messages -> zero model calls, nothing advised"


def scenario_3_only_outbound():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs = _seed(Path(td))
        ep.log_ben_contact(invocation_num=4, direction="in", channel="dm", body="(operator) hello")
        ep.log_ben_contact(invocation_num=4, direction="out", channel="chat", body="I have a question for you.")
        ep.log_ben_contact(invocation_num=4, direction="out", channel="agent_channel", body="(journal, not to operator)")
        res = run_advisory_pass(instance=inst, episodic=ep, research_store=rs, slack=FakeSlack(),
                                anthropic_client=FakeClient([ADVISORY]), agent_root=agent_root)
        assert res["advised"] == 1, res   # only the dm/chat outbound, not inbound or journal
    return "advises only outbound dm/chat messages (not inbound or journal posts)"


def scenario_4_budget_zero():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs = _seed(Path(td), advisory_overrides={"advisory_budget_cap_usd": 0.0})
        ep.log_ben_contact(invocation_num=4, direction="out", channel="dm", body="hi")
        client = FakeClient([ADVISORY])  # must NOT be consumed
        res = run_advisory_pass(instance=inst, episodic=ep, research_store=rs, slack=FakeSlack(),
                                anthropic_client=client, agent_root=agent_root)
        assert res["advised"] == 0 and len(client.queue) == 1, res
        assert ep.get_meta("last_advised_ben_contact_id", "0") in ("0", None)
    return "budget=0 -> no model calls, marker not advanced (retried next pass)"


def scenario_5_parse_failure_stub():
    with tempfile.TemporaryDirectory() as td:
        agent_root, inst, ep, rs = _seed(Path(td))
        cid = ep.log_ben_contact(invocation_num=4, direction="out", channel="dm", body="hi")
        slack = FakeSlack()
        res = run_advisory_pass(instance=inst, episodic=ep, research_store=rs, slack=slack,
                                anthropic_client=FakeClient(["not json at all"]), agent_root=agent_root)
        # generation "succeeded" (call made) but parse failed -> stored stub, not posted, marker advanced
        assert res["advised"] == 1 and res["posted"] == 0, res
        assert len(slack.advisory_posts) == 0
        assert rs.has_advisory(cid) and int(ep.get_meta("last_advised_ben_contact_id")) == cid
    return "unparseable advisory -> stored stub, not posted, marker advances (no reprocess loop)"


SCENARIOS = [
    ("1: happy path", scenario_1_happy_path),
    ("2: idempotent / no new", scenario_2_idempotent_no_new),
    ("3: only outbound advised", scenario_3_only_outbound),
    ("4: budget zero", scenario_4_budget_zero),
    ("5: parse failure stub", scenario_5_parse_failure_stub),
]


def test_scenario_1_happy_path(): scenario_1_happy_path()
def test_scenario_2_idempotent_no_new(): scenario_2_idempotent_no_new()
def test_scenario_3_only_outbound(): scenario_3_only_outbound()
def test_scenario_4_budget_zero(): scenario_4_budget_zero()
def test_scenario_5_parse_failure_stub(): scenario_5_parse_failure_stub()


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
