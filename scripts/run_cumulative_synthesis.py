#!/usr/bin/env python3
"""Refresh the rolling cumulative synthesis report for an instance (on demand).

Re-derives the living report from the stored per-session notes (+ a deterministic
evidence table + raw spot-checks) via a panel of lenses -> red-team -> chair. Makes
REAL Anthropic calls (bounded by config research.cumulative_budget_cap_usd) and
writes a new version into research_cumulative. Never edits the spec.

Usage:
    ./venv/bin/python scripts/run_cumulative_synthesis.py <instance_id>
    ./venv/bin/python scripts/run_cumulative_synthesis.py <instance_id> --no-embed
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(AGENT_ROOT / ".env", override=True)

import anthropic  # noqa: E402

from instances_common import SHARED_HF_CACHE, load_instance  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402
from research.store import ResearchStore  # noqa: E402
from research.synthesis import run_cumulative_synthesis  # noqa: E402

os.environ.setdefault("HF_HOME", str(SHARED_HF_CACHE))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Refresh the cumulative synthesis report.")
    p.add_argument("instance_id")
    p.add_argument("--no-embed", action="store_true",
                   help="skip embedding the report summary (faster, no model load)")
    args = p.parse_args(argv)

    try:
        inst = load_instance(args.instance_id)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    ep = EpisodicStore(inst.episodes_db)
    rs = ResearchStore(inst.episodes_db)
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    semantic = None
    if not args.no_embed:
        from memory.semantic import SemanticStore
        semantic = SemanticStore(inst.vectors_dir)

    print(f"Refreshing cumulative synthesis for '{inst.id}' (real API spend)...")
    res = run_cumulative_synthesis(
        instance=inst, episodic=ep, research_store=rs,
        anthropic_client=client, agent_root=AGENT_ROOT, semantic=semantic,
    )
    if not res:
        print("  no report produced (no spec / no coding scheme / disabled — see logs).")
        return 0
    print(f"  {res.get('status')} · n_sessions={res.get('n_sessions')} "
          f"cost=${res.get('cost_usd', 0):.4f} drift_flagged={res.get('drift_flagged')}"
          f"{' [degraded]' if res.get('degraded') else ''}")
    latest = rs.latest_cumulative(inst.id)
    if latest and latest.get("report", {}).get("executive_summary"):
        print("\n--- executive summary ---\n" + latest["report"]["executive_summary"])
    print("\nView on the dashboard `research` tab.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
