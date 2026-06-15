"""Re-code sessions with the artifact-aware instrument, archiving the original first
(replace-with-compare, Stage C).

For each target session: snapshot the current coding -> archive it -> re-run the
research panel (which REPLACES the canonical coding now that it can see the agent's
authored output) -> compare original vs new and print the divergence. Optionally
refresh the cumulative synthesis once at the end.

REAL API SPEND, bounded by the per-run budget caps in the instance's research config
(budget_cap_usd for the panel; a separate cap for cumulative). Use --dry-run first.

    ./venv/bin/python scripts/recode_with_compare.py v5-recollection --dry-run
    ./venv/bin/python scripts/recode_with_compare.py v5-recollection [--sessions 1,2,3] [--cumulative]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research.clients import research_clients  # noqa: E402
from research.panel import run_research_panel  # noqa: E402
from research.recode_compare import compare_codings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("instance_id")
    ap.add_argument("--sessions", help="comma-separated session ids; default: all sessions")
    ap.add_argument("--label", default="pre_artifact_recode", help="archive label for the originals")
    ap.add_argument("--cumulative", action="store_true", help="refresh cumulative synthesis at the end")
    ap.add_argument("--dry-run", action="store_true", help="no spend: list targets + current coding only")
    args = ap.parse_args()

    rc = research_clients(args.instance_id, embed=False)  # no embedding model load / re-embed
    inst, ep, rs = rc.instance, rc.episodic, rc.research

    by_id = {s["id"]: s for s in ep.all_sessions()}
    ids = [int(x) for x in args.sessions.split(",")] if args.sessions else list(by_id)
    targets = [by_id[i] for i in ids if i in by_id]
    cap = (inst.config.get("research") or {}).get("budget_cap_usd", "?")
    print(f"instance={inst.id} model={inst.model} | per-panel budget cap=${cap} | "
          f"targets={[t['id'] for t in targets]}")

    if args.dry_run:
        for t in targets:
            snap = rs.snapshot_session_coding(t["id"])
            print(f"  session#{t['id']} inv={t.get('invocation_num')}: existing coding = "
                  f"{len(snap['seat_notes'])} seat notes, note={'yes' if snap['note'] else 'no'}")
        n = len(targets)
        print(f"\n[dry-run] no API calls. A live run recodes {n} session(s) "
              f"(3 seats + chair each) + {'1 cumulative' if args.cumulative else 'no cumulative'}; "
              f"hard ceiling ~${n} x panel-cap (+ cumulative cap). Re-run without --dry-run.")
        return 0

    reports = []
    for t in targets:
        sid, inv = t["id"], t.get("invocation_num")
        original = rs.snapshot_session_coding(sid)
        if original["seat_notes"] or original["note"]:
            rs.archive_session_coding(sid, invocation_num=inv, label=args.label, snapshot=original)
        # replace: the panel clears + writes the fresh canonical coding
        run_research_panel(
            instance=inst, episodic=ep, research_store=rs, anthropic_client=rc.anthropic,
            session_id=sid, invocation_num=inv, agent_root=rc.agent_root,
            semantic=None, also_cumulative=False,
        )
        rep = compare_codings(original, rs.snapshot_session_coding(sid))
        rep["session_id"], rep["invocation_num"] = sid, inv
        reports.append(rep)
        print(f"\n=== session#{sid} (inv {inv}) — {rep['n_flips']} code flip(s) ===")
        for f in rep["flips"]:
            print(f"    {f['direction']:16} {f['code']}  ({f['original']} -> {f['new']})")
        a = rep["analytic_layer_new"]
        print(f"  channels={a['channels']} registers={a['registers']} "
              f"discrepancies={a['discrepancies']} overlaps={a['overlaps']} "
              f"present-codes-citing-artifacts={a['present_codes_citing_artifacts']}")

    if args.cumulative:
        from research.synthesis import run_cumulative_synthesis
        print("\nrefreshing cumulative synthesis ...")
        run_cumulative_synthesis(
            instance=inst, episodic=ep, research_store=rs, anthropic_client=rc.anthropic,
            agent_root=rc.agent_root, semantic=None,
        )

    total_flips = sum(r["n_flips"] for r in reports)
    print(f"\nDONE. Recoded {len(reports)} session(s); {total_flips} total code flip(s). "
          f"Originals archived under label='{args.label}'. Spend logged to cost_tracking.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
