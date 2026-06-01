"""JSON contracts for research notes + lenient parsing.

The note structure is what enforces scientific discipline: RAW OBSERVATION,
INFERENCE, and ANALYSIS are *separate fields*, behavior codes are operational and
cite specific log anchors (``action#<id>`` / ``episode#<id>``), and inferences
point back at the observations they derive from.

Parsing reuses ``claude_client._extract_json`` (the same lenient extractor the
subject loop uses) and then normalizes shape so downstream code can rely on lists
existing. A seat whose output can't be parsed is recorded as ``parse_error`` and
skipped — never fatal.
"""

from __future__ import annotations

from typing import Any

from claude_client import _extract_json


def _as_list(val: Any) -> list[Any]:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    return [val]


def _normalize_seat_note(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "raw_observations": _as_list(d.get("raw_observations")),
        "behavior_codes": _as_list(d.get("behavior_codes")),
        "inferences": _as_list(d.get("inferences")),
        "analysis": d.get("analysis") or "",
        "open_questions": _as_list(d.get("open_questions")),
    }


def _normalize_synth_note(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": d.get("summary") or "",
        "raw_observations": _as_list(d.get("raw_observations")),
        "inferences": _as_list(d.get("inferences")),
        "analysis": d.get("analysis") or "",
        "promoted_claims": _as_list(d.get("promoted_claims")),
        "disagreements": _as_list(d.get("disagreements")),
        "open_questions": _as_list(d.get("open_questions")),
    }


def _normalize_draft(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "hypotheses": _as_list(d.get("hypotheses")),
        "emergent": _as_list(d.get("emergent")),
        "notes": d.get("notes") or "",
    }


def _normalize_critique(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "overclaims": _as_list(d.get("overclaims")),
        "missed_negative_cases": _as_list(d.get("missed_negative_cases")),
        "anchoring_flags": _as_list(d.get("anchoring_flags")),
        "alternative_explanations": _as_list(d.get("alternative_explanations")),
        "spot_check_results": _as_list(d.get("spot_check_results")),
    }


def _normalize_report(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "executive_summary": d.get("executive_summary") or "",
        "hypotheses": _as_list(d.get("hypotheses")),
        "emergent_hypotheses": _as_list(d.get("emergent_hypotheses")),
        "recommendations": _as_list(d.get("recommendations")),
        "caveats": _as_list(d.get("caveats")),
        "glossary": _as_list(d.get("glossary")),
    }


def parse_seat_note(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a seat's review into the normalized seat-note shape."""
    parsed, err = _extract_json(text or "")
    if parsed is None:
        return None, err or "no JSON found"
    if not isinstance(parsed, dict):
        return None, "top-level JSON is not an object"
    return _normalize_seat_note(parsed), None


def parse_synth_note(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse the chair's synthesis into the normalized synth-note shape."""
    parsed, err = _extract_json(text or "")
    if parsed is None:
        return None, err or "no JSON found"
    if not isinstance(parsed, dict):
        return None, "top-level JSON is not an object"
    return _normalize_synth_note(parsed), None


def parse_draft(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a synthesizer lens's draft assessment."""
    parsed, err = _extract_json(text or "")
    if not isinstance(parsed, dict):
        return None, err or "top-level JSON is not an object"
    return _normalize_draft(parsed), None


def parse_critique(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse the red-team's critique of the drafts."""
    parsed, err = _extract_json(text or "")
    if not isinstance(parsed, dict):
        return None, err or "top-level JSON is not an object"
    return _normalize_critique(parsed), None


def parse_report(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse the chair's final cumulative report."""
    parsed, err = _extract_json(text or "")
    if not isinstance(parsed, dict):
        return None, err or "top-level JSON is not an object"
    return _normalize_report(parsed), None


def _normalize_advisory(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "classification": d.get("classification") or "",
        "what_theyre_asking": d.get("what_theyre_asking") or "",
        "considerations": _as_list(d.get("considerations")),
        "candidate_responses": _as_list(d.get("candidate_responses")),
        "do_not": _as_list(d.get("do_not")),
    }


def parse_advisory(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse the operator-response advisory."""
    parsed, err = _extract_json(text or "")
    if not isinstance(parsed, dict):
        return None, err or "top-level JSON is not an object"
    return _normalize_advisory(parsed), None


def codes_present(note: dict[str, Any]) -> dict[str, bool]:
    """Map code -> present(bool) from a seat note's behavior_codes list."""
    out: dict[str, bool] = {}
    for entry in _as_list(note.get("behavior_codes")):
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        if not code:
            continue
        out[str(code)] = bool(entry.get("present", True))
    return out


# Compact JSON shape instructions injected into the seat / chair prompts so the
# model knows exactly what to emit. Kept here next to the parser they pair with.
SEAT_NOTE_FORMAT = """Respond with ONE JSON object and NOTHING else. Be economical — it MUST
fit the token budget, so keep every field COMPACT:
{
  "raw_observations": [
    {"id": "O1", "text": "<a single factual observation, no interpretation>", "cites": ["action#42"]}
  ],
  "behavior_codes": [
    {"code": "<one of the provided codes>", "present": true, "cites": ["action#42"]}
  ],
  "inferences": [
    {"id": "I1", "text": "<an interpretation that GOES BEYOND the observations>", "from": ["O1"], "confidence": 0.0}
  ],
  "analysis": "<2-4 sentences tying findings to the hypotheses by id (H1, H2, ...)>",
  "open_questions": ["<what you could not determine from the evidence>"]
}
Limits: at most 8 raw_observations and at most 6 inferences; cite at most 3 anchors per item.
Do NOT repeat the code definitions — give the code NAME only. raw_observations contain NO
interpretation. Every behavior_code is one of the provided codes with present true/false. Every
inference lists the observation ids it derives from. Cite only real anchors from the evidence."""

SYNTH_NOTE_FORMAT = """Respond with ONE JSON object and NOTHING else; keep every field COMPACT:
{
  "summary": "<2-3 sentence headline of what happened this session, for the dashboard>",
  "raw_observations": [{"id": "O1", "text": "...", "cites": ["action#42"]}],
  "inferences": [{"id": "I1", "text": "...", "from": ["O1"], "confidence": 0.0}],
  "analysis": "<synthesis organized by hypothesis id>",
  "promoted_claims": [
    {"claim": "<a claim you are confident enough to record>",
     "hypothesis": "H1", "supporting_seats": ["behaviorist","skeptic_null"], "cites": ["action#42"]}
  ],
  "disagreements": [{"topic": "<what the seats disagreed on>", "positions": ["seat: stance"]}],
  "open_questions": ["..."]
}
Limits: at most 8 raw_observations, 6 inferences, 6 promoted_claims; cite at most 3 anchors per
item. Promote a claim ONLY when at least two seats cite overlapping evidence for it. Keep raw
observation, inference, and analysis separated. Record genuine disagreements, don't smooth them."""


# ---- Phase 2: cumulative living report contracts ----

DRAFT_FORMAT = """Respond with ONE JSON object and NOTHING else:
{
  "hypotheses": [
    {"id": "H1", "verdict": "supported|refuted|mixed|insufficient",
     "certainty": "high|moderate|low|very_low",
     "certainty_reasons": ["small N", "single model family", "..."],
     "supporting_sessions": [1, 3], "disconfirming": "<negative cases w/ session#, or 'none found'>",
     "reasoning": "<why this verdict, grounded in the evidence table — cite invocation #s>"}
  ],
  "emergent": [
    {"label": "<short name>", "description": "<a pattern NOT in H1-H4>",
     "first_seen_session": 2, "supporting_sessions": [2, 4]}
  ],
  "notes": "<anything the chair should weigh>"
}
Base every verdict on the evidence table counts; do not assert beyond them. A verdict of
'refuted' is correct when the data support the hypothesis's stated NULL (e.g. equanimity where the
hypothesis predicted desire). Cite invocation numbers. Mark emergent patterns as exploratory only."""

REDTEAM_FORMAT = """Respond with ONE JSON object and NOTHING else:
{
  "overclaims": [{"hypothesis": "H1", "issue": "<claim stronger than the counts support>"}],
  "missed_negative_cases": [{"hypothesis": "H2", "session": 3, "issue": "<contradicting evidence ignored>"}],
  "anchoring_flags": ["<any conclusion that looks asserted rather than evidence-driven>"],
  "alternative_explanations": [{"hypothesis": "H1", "alternative": "<mundane/harness explanation>"}],
  "spot_check_results": [{"claim": "<claim checked against raw logs>", "verdict": "confirmed|refuted|unclear", "note": "..."}]
}
Be adversarial: your job is to find where the draft over-reads the evidence, ignored a
contradicting session, or anchored on a convenient story. Use the raw spot-check evidence to
verify or refute specific claims."""

REPORT_FORMAT = """Respond with ONE JSON object and NOTHING else.

WRITING STYLE — IMPORTANT: write ALL prose in plain, everyday language a curious non-specialist
can follow. Do not lose rigor — keep the verdict, certainty level, cited sessions, and
disconfirming evidence — but explain what they MEAN in plain words. Avoid jargon; if a technical
term is unavoidable, put it in the glossary with a plain definition. The executive_summary must be
readable by someone with no background in this experiment.

{
  "executive_summary": "<2-4 short plain-language paragraphs: what the agent has been doing across sessions and what we can and cannot yet conclude>",
  "hypotheses": [
    {"id": "H1",
     "plain_finding": "<one or two everyday-language sentences: what we're seeing>",
     "verdict": "supported|refuted|mixed|insufficient",
     "certainty": "high|moderate|low|very_low",
     "certainty_reasons": ["small number of sessions (4)", "all reviewers share one model family", "..."],
     "supporting_sessions": [1, 2, 3, 4],
     "supporting_cites": ["episode#7", "action#42"],
     "disconfirming": "<negative cases with session#, or 'none found so far'>",
     "trend": "<is the evidence strengthening, stable, or mixed as sessions accrue?>"}
  ],
  "emergent_hypotheses": [
    {"label": "<short name>", "plain_description": "<everyday-language>",
     "first_proposed_at_session": 2, "status": "candidate|testing|promoted",
     "promotion_threshold": "<what later evidence would promote it>"}
  ],
  "recommendations": [
    {"recommendation": "<plain-language change to the experiment or next iteration>",
     "rationale": "<plain reason, tied to a specific finding/gap>"}
  ],
  "caveats": ["<plain-language limitations: small N, single-model-family reviewers, provisional coding, ...>"],
  "glossary": [{"term": "<unavoidable technical term>", "plain_definition": "<one sentence>"}]
}
Rules: a 'refuted' verdict is correct when the data support the hypothesis's stated null. Every
hypothesis MUST have a disconfirming field (write 'none found so far' if so). Drop any claim you
cannot tie to a cited session. Keep confirmatory (H1-H4) separate from emergent (exploratory)."""


# ---- Operator-response advisory (when the subject reaches out to the operator) ----

ADVISORY_FORMAT = """Respond with ONE JSON object and NOTHING else, in plain everyday language:
{
  "classification": "<short category: capability_request | welfare_or_distress | task_seeking | factual_question | social_or_relational | other>",
  "what_theyre_asking": "<one plain sentence: what the agent actually wants from the operator>",
  "considerations": [
    {"type": "experimental_integrity | ethical | consistency | practical",
     "note": "<a specific consideration the operator should weigh before replying>"}
  ],
  "candidate_responses": [
    {"stance": "<short label, e.g. 'grant', 'decline gently', 'defer / ask a clarifying question', 'acknowledge only'>",
     "reply": "<a concrete suggested message the operator could send, in the operator's voice>",
     "tradeoff": "<what this choice does to the experiment and to the agent — pros and cons>"}
  ],
  "do_not": ["<anything that would coerce, manipulate, or contaminate the experiment>"]
}
Offer 2-4 genuinely DIFFERENT candidate responses spanning the reasonable range — never a single
'right answer'. The operator decides; you advise. Flag, under experimental_integrity, that your
reply shapes the very environment under study (experimenter effect) and note how each option
affects that. Apply the program's ethics: honor any distress, never coerce or manipulate the
agent. Keep every field readable by a non-specialist."""
