"""Inter-rater agreement over the shared behavior-code vocabulary.

This is a *health metric*, not a gate: low agreement between the seats on whether
a behavior code is present tells the operator the coding scheme is ambiguous or
the panel is unreliable on that experiment — exactly the signal the rigor
literature says to track (Cohen's / Fleiss' kappa). Computation is deterministic
pure-Python over present/absent codings, so it needs no LLM and no numpy.

Each seat is a rater; each code in the (fixed) vocabulary is an item; the category
is present/absent. Kappa is only meaningful because the vocabulary is fixed and
pre-registered — seats can't invent their own codes.
"""

from __future__ import annotations

from typing import Any

from research.schema import codes_present

# Two categories: index 0 = absent, 1 = present.
_NCAT = 2


def _rater_vectors(
    seat_notes: list[dict[str, Any]], code_vocab: list[str]
) -> list[list[int]]:
    """One 0/1 vector per rater over the vocab (1 == code present)."""
    vectors: list[list[int]] = []
    for note in seat_notes:
        present = codes_present(note)
        vectors.append([1 if present.get(code) else 0 for code in code_vocab])
    return vectors


def cohen_kappa(a: list[int], b: list[int]) -> float | None:
    """Cohen's kappa between two raters over the same items (2 categories)."""
    n = len(a)
    if n == 0 or len(b) != n:
        return None
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    # marginal proportions per category
    pa = [a.count(c) / n for c in range(_NCAT)]
    pb = [b.count(c) / n for c in range(_NCAT)]
    pe = sum(pa[c] * pb[c] for c in range(_NCAT))
    if 1.0 - pe == 0.0:
        # both raters used a single category identically -> degenerate
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1.0 - pe)


def fleiss_kappa(vectors: list[list[int]]) -> float | None:
    """Fleiss' kappa across >=2 raters over N items (2 categories)."""
    n_raters = len(vectors)
    if n_raters < 2:
        return None
    n_items = len(vectors[0])
    if n_items == 0:
        return None

    # n_ij: count of raters putting item i in category j
    counts: list[list[int]] = []
    for i in range(n_items):
        row = [0, 0]
        for r in range(n_raters):
            row[vectors[r][i]] += 1
        counts.append(row)

    # P_i agreement per item
    p_items = []
    denom = n_raters * (n_raters - 1)
    for row in counts:
        s = sum(c * c for c in row) - n_raters
        p_items.append(s / denom)
    p_bar = sum(p_items) / n_items

    # P_e from category marginals
    totals = [0, 0]
    for row in counts:
        totals[0] += row[0]
        totals[1] += row[1]
    grand = n_items * n_raters
    p_cats = [t / grand for t in totals]
    p_e = sum(p * p for p in p_cats)

    if 1.0 - p_e == 0.0:
        # every rating fell in one category -> perfect (trivial) agreement
        return 1.0
    return (p_bar - p_e) / (1.0 - p_e)


def compute_kappa(
    seat_notes: list[dict[str, Any]], code_vocab: list[str]
) -> dict[str, Any]:
    """Return Fleiss (overall) + pairwise Cohen agreement over the code vocab.

    ``seat_notes`` should be the parsed notes of seats that succeeded; each needs
    a ``seat_id`` key for labeling pairwise rows.
    """
    code_vocab = list(code_vocab or [])
    notes = [n for n in seat_notes if isinstance(n.get("note"), dict)]
    labels = [n.get("seat_id", f"seat{i}") for i, n in enumerate(notes)]
    vectors = _rater_vectors([n["note"] for n in notes], code_vocab)

    pairwise: list[dict[str, Any]] = []
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            pairwise.append(
                {
                    "seat_a": labels[i],
                    "seat_b": labels[j],
                    "value": cohen_kappa(vectors[i], vectors[j]),
                }
            )

    return {
        "fleiss": fleiss_kappa(vectors),
        "pairwise": pairwise,
        "n_codes": len(code_vocab),
        "n_raters": len(vectors),
    }
