#!/usr/bin/env python3
"""Put a design proposal to the research panel and print its verdict.

Reads the proposal from a file or inline text, runs it through the multi-lens
review (research/proposal_review.py), stores it in research_proposals, and prints
the chair's plain-language synthesis. Makes REAL Anthropic calls.

Usage:
    ./venv/bin/python scripts/review_proposal.py --instance v4-continuous --file proposal.md
    ./venv/bin/python scripts/review_proposal.py --instance v4-continuous --text "..."
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_ROOT))

from research.clients import research_clients  # noqa: E402
from research.proposal_review import run_proposal_review  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Put a design proposal to the research panel.")
    p.add_argument("--instance", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--file", help="path to a file containing the proposal")
    g.add_argument("--text", help="the proposal as inline text")
    args = p.parse_args(argv)

    proposal = Path(args.file).read_text(encoding="utf-8") if args.file else args.text
    try:
        # Proposal review does not embed anything; skip the model load.
        rc = research_clients(args.instance, embed=False)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    res = run_proposal_review(
        instance=rc.instance, episodic=rc.episodic,
        research_store=rc.research, anthropic_client=rc.anthropic,
        agent_root=rc.agent_root, proposal_text=proposal,
    )
    if not res:
        print("no review produced (see logs).")
        return 0
    print(f"\n=== panel review (proposal #{res['proposal_id']}) — "
          f"recommendation: {res['recommendation']} · ${res['cost_usd']:.4f}"
          f"{' [degraded]' if res['degraded'] else ''} ===\n")
    print(json.dumps(res["review"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
