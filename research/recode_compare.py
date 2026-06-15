"""Compare two codings of the same session — the divergence report.

A 'replace' re-code (with the artifact-aware instrument) overwrites the canonical
coding, but we archive the original first (``ResearchStore.archive_session_coding``)
so we can answer the instrument-validity question: *did giving the panel the agent's
authored output change what it coded?* Pure functions over snapshot dicts
(``{seat_notes, note, kappa}``) — no LLM, no I/O.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from research.schema import codes_present


def _present_fractions(seat_notes: list[dict[str, Any]]) -> tuple[dict[str, tuple[int, int]], int]:
    """code -> (n_seats_marking_present, n_seats_total) across a coding's seat notes."""
    notes = [sn.get("note") for sn in (seat_notes or []) if sn.get("note")]
    n = len(notes)
    codes: set[str] = set()
    for note in notes:
        codes.update(codes_present(note).keys())
    frac: dict[str, tuple[int, int]] = {}
    for c in codes:
        present = sum(1 for note in notes if codes_present(note).get(c, False))
        frac[c] = (present, n)
    return frac, n


def _majority(present: int, total: int) -> bool:
    return total > 0 and present * 2 > total


def _analytic_summary(seat_notes: list[dict[str, Any]]) -> dict[str, Any]:
    """The new metadata layer, present only in artifact-aware codings."""
    channels: Counter = Counter()
    registers: Counter = Counter()
    discrepancies = 0
    overlaps = 0
    artifact_cites = 0
    for sn in seat_notes or []:
        note = sn.get("note") or {}
        for bc in note.get("behavior_codes", []):
            if not isinstance(bc, dict) or not bc.get("present"):
                continue
            ch = bc.get("channel")
            if ch:
                channels[ch] += 1
            if bc.get("register"):
                registers[bc["register"]] += 1
            if any(str(a).startswith(("memory#", "doc#")) for a in (bc.get("cites") or [])):
                artifact_cites += 1
        discrepancies += len(note.get("discrepancies") or [])
        overlaps += len(note.get("overlaps") or [])
    return {
        "channels": dict(channels),
        "registers": dict(registers),
        "discrepancies": discrepancies,
        "overlaps": overlaps,
        "present_codes_citing_artifacts": artifact_cites,
    }


def compare_codings(original: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Divergence between an original (journal-only) and new (artifact-aware) coding.

    Reports per-code majority flips (the headline: which behaviors the panel now
    marks differently once it can see the agent's authored output) plus the new
    analytic layer the original lacked.
    """
    o_frac, o_n = _present_fractions(original.get("seat_notes", []))
    n_frac, n_n = _present_fractions(new.get("seat_notes", []))
    flips: list[dict[str, Any]] = []
    for code in sorted(set(o_frac) | set(n_frac)):
        op, ot = o_frac.get(code, (0, 0))
        np_, nt = n_frac.get(code, (0, 0))
        o_maj, n_maj = _majority(op, ot), _majority(np_, nt)
        if o_maj != n_maj:
            flips.append({
                "code": code,
                "original": f"{op}/{ot}",
                "new": f"{np_}/{nt}",
                "direction": "absent→present" if n_maj else "present→absent",
            })
    return {
        "n_seats": {"original": o_n, "new": n_n},
        "n_flips": len(flips),
        "flips": flips,
        "analytic_layer_new": _analytic_summary(new.get("seat_notes", [])),
    }
