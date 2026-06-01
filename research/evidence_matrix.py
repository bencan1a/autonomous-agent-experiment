"""Deterministic cross-session evidence matrix (pure, no LLM).

The provenance backbone of the cumulative report: from the stored binding
per-session notes it tallies, per hypothesis, which sessions show each behavior
code present/absent (majority of seats), the per-session Fleiss kappa, the
panel's promoted claims grouped by hypothesis, and recorded disagreements. This
quantitative table is the ground truth the synthesis narrative must not
contradict, and it also selects (deterministically) which sessions to pull RAW
evidence for during the red-team spot-check stage.

It intentionally does NOT decide a hypothesis "verdict" — code polarity
(confirming vs. the null, e.g. H4's competing_desire vs. equanimity_at_winddown)
is for the LLM to interpret from the hypothesis statement + code definitions.
The matrix only counts.
"""

from __future__ import annotations

from math import ceil
from typing import Any

from research.schema import codes_present

_LOW_KAPPA = 0.6          # sessions below this are disagreement spot-check candidates
_MAX_SPOT_CHECKS = 5      # bound the raw-evidence pulls (cost)
_MAX_CITES = 4


def _fleiss_for(research_store, session_id: int) -> float | None:
    for k in research_store.kappa_for_session(session_id):
        if k.get("metric") == "fleiss":
            return k.get("value")
    return None


def build_evidence_matrix(research_store, prereg: dict[str, Any]) -> dict[str, Any]:
    code_vocab = prereg.get("code_vocab") or []
    hyp_of_code: dict[str, str | None] = {}
    def_of_code: dict[str, str] = {}
    codes_by_hyp: dict[str, list[str]] = {}
    for c in code_vocab:
        if not isinstance(c, dict) or not c.get("code"):
            continue
        code = str(c["code"])
        h = c.get("maps_to_hypothesis")
        hyp_of_code[code] = h
        def_of_code[code] = c.get("definition", "")
        codes_by_hyp.setdefault(h, []).append(code)

    hypotheses = prereg.get("hypotheses") or []

    all_notes = research_store.recent_notes(n=100000)
    binding = [n for n in all_notes if n.get("coding_status") == "binding"]
    # Fall back to all notes only if nothing has been approved yet (keeps the
    # report non-empty while clearly flagging the provisional status downstream).
    used = binding or all_notes
    used = sorted(used, key=lambda n: (n.get("invocation_num") or 0))

    # Per-session majority-present codes + citations.
    sessions: list[dict[str, Any]] = []
    present_map: dict[int, dict[str, bool]] = {}   # invnum -> code -> present
    cites_map: dict[int, dict[str, list[str]]] = {}
    for n in used:
        sid = n["session_id"]
        inv = n.get("invocation_num")
        seats = [s for s in research_store.seat_notes_for_session(sid)
                 if s.get("status") == "ok" and isinstance(s.get("note"), dict)]
        n_ok = len(seats)
        threshold = ceil(n_ok / 2) if n_ok else 1
        tally: dict[str, int] = {}
        cites: dict[str, list[str]] = {}
        for s in seats:
            pres = codes_present(s["note"])
            for bc in s["note"].get("behavior_codes", []):
                if isinstance(bc, dict) and bc.get("code") and pres.get(str(bc["code"])):
                    code = str(bc["code"])
                    for a in (bc.get("cites") or []):
                        if a not in cites.setdefault(code, []):
                            cites[code].append(a)
            for code, p in pres.items():
                tally[code] = tally.get(code, 0) + (1 if p else 0)
        present_map[inv] = {code: (cnt >= threshold and n_ok > 0) for code, cnt in tally.items()}
        cites_map[inv] = {c: v[:_MAX_CITES] for c, v in cites.items()}
        sessions.append({
            "session_id": sid,
            "invocation_num": inv,
            "fleiss": _fleiss_for(research_store, sid),
            "coding_status": n.get("coding_status"),
        })

    inv_to_sid = {s["invocation_num"]: s["session_id"] for s in sessions}

    # Per-hypothesis code presence + promoted claims + disagreements.
    hyp_rows: list[dict[str, Any]] = []
    sole_support_sessions: set[int] = set()
    for h in hypotheses:
        hid = h.get("id")
        rows = []
        for code in codes_by_hyp.get(hid, []):
            present_invs = [s["invocation_num"] for s in sessions
                            if present_map.get(s["invocation_num"], {}).get(code)]
            absent_invs = [s["invocation_num"] for s in sessions
                           if code in present_map.get(s["invocation_num"], {})
                           and not present_map[s["invocation_num"]][code]]
            if len(present_invs) == 1:
                sole_support_sessions.add(present_invs[0])
            rows.append({
                "code": code,
                "definition": def_of_code.get(code, ""),
                "present_sessions": present_invs,
                "absent_sessions": absent_invs,
                "present_count": len(present_invs),
                "n": len(sessions),
                "cites": {inv: cites_map.get(inv, {}).get(code, [])
                          for inv in present_invs},
            })
        promoted = []
        for n in used:
            for pc in (n.get("note") or {}).get("promoted_claims", []) or []:
                if isinstance(pc, dict) and pc.get("hypothesis") == hid:
                    promoted.append({
                        "invocation_num": n.get("invocation_num"),
                        "claim": pc.get("claim"),
                        "supporting_seats": pc.get("supporting_seats"),
                        "cites": (pc.get("cites") or [])[:_MAX_CITES],
                    })
        hyp_rows.append({
            "id": hid,
            "statement": h.get("statement"),
            "predicted_evidence": h.get("predicted_evidence"),
            "falsifying_evidence": h.get("falsifying_evidence"),
            "codes": rows,
            "promoted_claims": promoted,
        })

    disagreements = []
    for n in used:
        for d in (n.get("note") or {}).get("disagreements", []) or []:
            if isinstance(d, dict) and d.get("topic"):
                disagreements.append({
                    "invocation_num": n.get("invocation_num"),
                    "topic": d.get("topic"),
                    "positions": d.get("positions"),
                })

    # Deterministic spot-check selection: sole-support sessions + low-kappa
    # sessions, most-recent first, bounded.
    low_kappa = {s["invocation_num"] for s in sessions
                 if s["fleiss"] is not None and s["fleiss"] < _LOW_KAPPA}
    spot_invs = sorted(sole_support_sessions | low_kappa, reverse=True)[:_MAX_SPOT_CHECKS]
    spot_checks = [{"invocation_num": inv, "session_id": inv_to_sid.get(inv)}
                   for inv in spot_invs if inv in inv_to_sid]

    return {
        "sessions": sessions,
        "n_sessions": len(sessions),
        "n_binding": len(binding),
        "n_provisional": len(all_notes) - len(binding),
        "hypotheses": hyp_rows,
        "disagreements": disagreements,
        "spot_check_sessions": spot_checks,
        "max_invocation": max((s["invocation_num"] for s in sessions), default=None),
    }
