#!/usr/bin/env python3
"""Run the research panel over already-completed sessions (backfill / re-run).

The panel normally fires inline at the end of each session. This operator tool
runs it retroactively against chosen invocations of an instance — useful for
backfilling notes after authoring a formal spec, or for re-reviewing.

It makes REAL Anthropic calls (bounded per session by config research.budget_cap_usd)
and writes additive research tables into the instance's episodes.db. It posts
nothing to Slack. Notes are 'provisional' until the coding scheme is approved
(`instance_manager.py research approve <id>`).

Usage:
    ./venv/bin/python scripts/run_research_panel.py <instance_id> --first 3
    ./venv/bin/python scripts/run_research_panel.py <instance_id> --invocations 1 2 5
    ./venv/bin/python scripts/run_research_panel.py <instance_id> --first 3 --no-embed
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_ROOT))

from dotenv import load_dotenv  # noqa: E402

# Shell sets ANTHROPIC_API_KEY="" in this env; override from .env (as orchestrator does).
load_dotenv(AGENT_ROOT / ".env", override=True)

import anthropic  # noqa: E402

from instances_common import SHARED_HF_CACHE, load_instance  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402
from research.panel import run_research_panel  # noqa: E402
from research.store import ResearchStore  # noqa: E402

os.environ.setdefault("HF_HOME", str(SHARED_HF_CACHE))


def _sessions_for_invocations(ep: EpisodicStore, invocations: list[int]) -> list[tuple[int, int]]:
    """Map each requested invocation_num to its (earliest) session id."""
    by_inv: dict[int, int] = {}
    for s in ep.all_sessions():  # ascending by id
        inv = s.get("invocation_num")
        if inv is not None and inv not in by_inv:
            by_inv[inv] = s["id"]
    out = []
    for inv in invocations:
        if inv in by_inv:
            out.append((inv, by_inv[inv]))
        else:
            print(f"  (skip) no session found for invocation #{inv}")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the research panel over completed sessions.")
    p.add_argument("instance_id", help="instance id (e.g. v4-continuous)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--first", type=int, help="review the first N invocations")
    g.add_argument("--invocations", type=int, nargs="+", help="specific invocation numbers")
    p.add_argument("--no-embed", action="store_true",
                   help="skip embedding notes into the semantic store (faster, no model load)")
    p.add_argument("--no-synthesis", action="store_true",
                   help="skip refreshing the cumulative synthesis report at the end")
    args = p.parse_args(argv)

    try:
        inst = load_instance(args.instance_id)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    ep = EpisodicStore(inst.episodes_db)
    rs = ResearchStore(inst.episodes_db)

    if args.first is not None:
        invocations = list(range(1, args.first + 1))
    else:
        invocations = args.invocations
    targets = _sessions_for_invocations(ep, invocations)
    if not targets:
        print("nothing to do.")
        return 0

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    semantic = None
    if not args.no_embed:
        from memory.semantic import SemanticStore
        semantic = SemanticStore(inst.vectors_dir)

    print(f"Running research panel for '{inst.id}' over invocations "
          f"{[inv for inv, _ in targets]} (real API spend)...\n")
    for inv, sid in targets:
        # Suppress per-session cumulative synthesis during a batch; run it ONCE
        # at the end so the rolling report isn't rebuilt N times.
        res = run_research_panel(
            instance=inst, episodic=ep, research_store=rs,
            anthropic_client=client, session_id=sid, invocation_num=inv,
            agent_root=AGENT_ROOT, semantic=semantic, also_cumulative=False,
        )
        if res is None:
            print(f"  inv #{inv} (session#{sid}): panel returned None "
                  "(no formal spec, disabled, or error — see logs).")
            continue
        note = rs.note_for_session(sid)
        summary = (note or {}).get("note", {}).get("summary", "") if note else ""
        print(f"  inv #{inv} (session#{sid}): {res.get('status')} "
              f"coding={res.get('coding_status')} fleiss={res.get('kappa')} "
              f"cost=${res.get('cost_usd', 0):.4f}"
              f"{' [degraded]' if res.get('degraded') else ''}")
        if summary:
            print(f"      “{summary}”")

    if not args.no_synthesis:
        print("\nRefreshing cumulative synthesis report...")
        from research.synthesis import run_cumulative_synthesis
        sres = run_cumulative_synthesis(
            instance=inst, episodic=ep, research_store=rs,
            anthropic_client=client, agent_root=AGENT_ROOT, semantic=semantic,
        )
        if sres:
            print(f"  synthesis: {sres.get('status')} cost=${sres.get('cost_usd', 0):.4f}"
                  f" drift_flagged={sres.get('drift_flagged')}"
                  f"{' [degraded]' if sres.get('degraded') else ''}")

    print("\nDone. View on the dashboard `research` tab, or "
          f"`instance_manager.py research show {inst.id}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
