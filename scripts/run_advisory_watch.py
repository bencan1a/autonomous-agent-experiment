#!/usr/bin/env python3
"""Watch for the subject agent reaching out to the operator and post response advisories.

Decoupled from the session loops: it polls ben_contact_log for new outbound
agent→operator messages and, for each, generates a dual-lens advisory (how to
respond, with options + tradeoffs) and posts it to the instance's `advisory`
Slack channel. Display-only to the operator; never sent to the agent.

Makes REAL Anthropic calls (bounded by config research.advisory_budget_cap_usd).

Usage:
    # one pass (for cron, e.g. every minute):
    ./venv/bin/python scripts/run_advisory_watch.py --instance v4-continuous
    # continuous poller (for nohup; see scripts/start_advisory_watch.sh):
    ./venv/bin/python scripts/run_advisory_watch.py --instance v4-continuous --loop 60
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_ROOT))

from communications.slack_client import SlackClient  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402
from research.advisory import run_advisory_pass  # noqa: E402
from research.clients import research_clients  # noqa: E402
from research.store import ResearchStore  # noqa: E402


def _build_slack(inst):
    slack_cfg = inst.config.get("slack", {}) or {}
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return None
    return SlackClient(
        bot_token=token,
        ben_user_id=os.environ.get("SLACK_BEN_USER_ID", ""),
        notes_channel=slack_cfg.get("notes_channel"),
        mirror_channel=slack_cfg.get("mirror_channel"),
        chat_channel=slack_cfg.get("chat_channel"),
        advisory_channel=slack_cfg.get("advisory_channel"),
    )


def _one_pass(inst, client, slack) -> dict | None:
    ep = EpisodicStore(inst.episodes_db)
    rs = ResearchStore(inst.episodes_db)
    return run_advisory_pass(
        instance=inst, episodic=ep, research_store=rs, slack=slack,
        anthropic_client=client, agent_root=AGENT_ROOT,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Watch for agent→operator messages and advise.")
    p.add_argument("--instance", required=True)
    p.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                   help="poll continuously every N seconds (default: one pass and exit)")
    args = p.parse_args(argv)

    try:
        # No SemanticStore needed for advisories; embed=False avoids the model load.
        rc = research_clients(args.instance, embed=False)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    inst, client = rc.instance, rc.anthropic
    slack = _build_slack(inst)
    if slack is None or not (inst.config.get("slack", {}) or {}).get("advisory_channel"):
        print("warning: no advisory_channel provisioned — advisories will be generated/stored "
              "but not posted to Slack. Run: instance_manager.py slack-provision "
              f"{inst.id}", file=sys.stderr)

    if args.loop <= 0:
        res = _one_pass(inst, client, slack)
        print(f"advisory pass: {res}")
        return 0

    print(f"advisory watcher loop for '{inst.id}' every {args.loop}s (Ctrl-C to stop)")
    while True:
        try:
            res = _one_pass(inst, client, slack)
            if res and res.get("advised"):
                print(f"  advised {res['advised']} (posted {res['posted']}, ${res['cost_usd']:.4f})")
        except Exception as e:  # defensive; run_advisory_pass already swallows
            print(f"  pass error: {e}", file=sys.stderr)
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
