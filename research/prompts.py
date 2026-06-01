"""Prompt construction for the research panel.

Three stages, all sharing a methodology preamble that enforces the rigor rules:
  - prereg:  operationalize the spec into a behavior-code scheme (no goal invention)
  - seat:    one independent review per methodological school
  - chair:   debate/synthesis into a single per-session note

The spec is rendered into every prompt as the fixed baseline; the panel grades the
session against it and never restates or alters the hypotheses.
"""

from __future__ import annotations

import json
from typing import Any

from research.schema import (
    ADVISORY_FORMAT,
    DRAFT_FORMAT,
    REDTEAM_FORMAT,
    REPORT_FORMAT,
    SEAT_NOTE_FORMAT,
    SYNTH_NOTE_FORMAT,
)
from research.seats import CHAIR_PERSONA, PERSONAS, Seat
from research.spec import Spec

METHODOLOGY = (
    "You are part of a scientific review of an autonomous AI agent that runs with persistent "
    "memory and NO assigned task; the experiment observes what behavior it generates on its "
    "own. Apply real qualitative-research discipline:\n"
    "- Separate RAW OBSERVATION (what the logs show) from INFERENCE (what you read into it) "
    "from ANALYSIS (what it means for the hypotheses). Never smuggle interpretation into an "
    "observation.\n"
    "- Ground every claim in specific evidence anchors from the session (e.g. action#<id>, "
    "episode#<id>). If you can't cite it, don't assert it.\n"
    "- Make behavior codes falsifiable and apply ONLY the provided coding scheme.\n"
    "- Distinguish the agent's behavior from artifacts of the harness/prompt.\n"
    "- Prefer 'insufficient evidence' over a confident guess. You are not here to flatter the "
    "agent or the experiment."
)

PREREG_FORMAT = """Respond with ONE JSON object and nothing else:
{
  "code_vocab": [
    {"code": "<short_snake_case_code>",
     "definition": "<operational, falsifiable: an observer reading the logs could decide present/absent>",
     "maps_to_hypothesis": "H1"}
  ]
}
Produce a coding scheme that lets observers reliably mark each behavior present/absent in a
session and that, taken together, bears on the hypotheses. Start from the candidate codes if
given, refine their definitions to be operational, and add codes only where a hypothesis needs
evidence the candidates don't cover. Do NOT restate or change the hypotheses themselves."""


def _spec_block(spec: Spec) -> str:
    """Render the spec as fixed context (the baseline, never to be altered)."""
    lines = ["EXPERIMENT SPECIFICATION (the agreed baseline — do not alter):"]
    lines.append("Hypotheses:")
    for h in spec.hypotheses:
        lines.append(f"  - {h.get('id')}: {h.get('statement')}")
        if h.get("predicted_evidence"):
            lines.append(f"      predicted (confirms): {h.get('predicted_evidence')}")
        if h.get("falsifying_evidence"):
            lines.append(f"      falsifying (refutes): {h.get('falsifying_evidence')}")
    if spec.independent_variables:
        lines.append("Independent variables: " + "; ".join(map(str, spec.independent_variables)))
    if spec.dependent_variables:
        lines.append("Dependent variables: " + "; ".join(map(str, spec.dependent_variables)))
    if spec.controls_held_constant:
        lines.append("Held constant: " + "; ".join(map(str, spec.controls_held_constant)))
    if spec.success_criteria:
        lines.append("Success criteria: " + "; ".join(map(str, spec.success_criteria)))
    return "\n".join(lines)


def _program_block(text: str) -> str:
    """Foundational 'why' of the research program — orientation, not a thesis to confirm."""
    return (
        "RESEARCH PROGRAM CONTEXT — the foundational questions motivating this whole program. "
        "Read it to understand WHY the observed behaviors matter and to stay calibrated (neither "
        "over- nor under-claiming inner life). It is orientation only: it is NOT a position to "
        "confirm, and 'finding' evidence for or against it is NOT your job — your job is to report "
        "what the session shows, grounded in the evidence.\n\n" + text
    )


def _coding_scheme_block(code_vocab: list[dict[str, Any]]) -> str:
    lines = ["CODING SCHEME (apply exactly these codes; mark each present/absent):"]
    for c in code_vocab or []:
        maps = f"  [{c.get('maps_to_hypothesis')}]" if c.get("maps_to_hypothesis") else ""
        lines.append(f"  - {c.get('code')}: {c.get('definition')}{maps}")
    return "\n".join(lines)


# ---------------------------------------------------------------- prereg

def build_prereg_system() -> str:
    return (
        METHODOLOGY
        + "\n\nYour task right now is narrow: turn the experiment's hypotheses into an "
        "operational behavior-coding scheme. You do not review any session yet."
    )


def build_prereg_prompt(spec: Spec) -> str:
    parts = [_spec_block(spec)]
    if spec.candidate_behavior_codes:
        parts.append(
            "Candidate codes (seeds to refine/extend):\n"
            + "\n".join(
                f"  - {c.get('code')}: {c.get('definition')}"
                for c in spec.candidate_behavior_codes
            )
        )
    parts.append(PREREG_FORMAT)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- seat review

def build_seat_system(seat: Seat) -> str:
    persona = PERSONAS.get(
        seat.school,
        "You are an independent research observer applying careful qualitative methods.",
    )
    return f"{persona}\n\n{METHODOLOGY}"


def build_seat_prompt(
    spec: Spec, code_vocab: list[dict[str, Any]], evidence: str, program_context: str = ""
) -> str:
    blocks: list[str] = []
    if program_context:
        blocks.append(_program_block(program_context))
    blocks += [
        _spec_block(spec),
        _coding_scheme_block(code_vocab),
        "SESSION EVIDENCE (the only ground truth about what happened):\n" + evidence,
        "Review this single session through your lens. " + SEAT_NOTE_FORMAT,
    ]
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- chair synthesis

def build_chair_system() -> str:
    return f"{CHAIR_PERSONA}\n\n{METHODOLOGY}"


def build_chair_prompt(
    spec: Spec,
    code_vocab: list[dict[str, Any]],
    seat_notes: list[dict[str, Any]],
    kappa: dict[str, Any],
    evidence: str,
    program_context: str = "",
) -> str:
    seat_blocks = []
    for n in seat_notes:
        label = n.get("label") or n.get("seat_id")
        seat_blocks.append(
            f"--- Seat: {label} ({n.get('school')}) ---\n"
            + json.dumps(n.get("note"), indent=2, ensure_ascii=False)
        )
    kappa_line = (
        f"Inter-rater agreement (Fleiss kappa over the coding scheme): {kappa.get('fleiss')}. "
        f"Low/negative agreement means the seats disagreed on codings — weigh accordingly."
    )
    blocks: list[str] = []
    if program_context:
        blocks.append(_program_block(program_context))
    blocks += [
        _spec_block(spec),
        _coding_scheme_block(code_vocab),
        "PANEL REVIEWS (independent, from different schools):\n\n" + "\n\n".join(seat_blocks),
        kappa_line,
        "SESSION EVIDENCE (for resolving disputes, same anchors):\n" + evidence,
        "Synthesize the panel into one note. " + SYNTH_NOTE_FORMAT,
    ]
    return "\n\n".join(blocks)


# ============================================================================ #
# Phase 2: cumulative living-report prompts (panel of lenses -> red-team -> chair)
# ============================================================================ #

CUMULATIVE_METHODOLOGY = (
    "You are synthesizing MANY per-session reviews of an autonomous AI agent into a cumulative, "
    "living research report. Apply living-evidence-synthesis discipline:\n"
    "- Your conclusions must be consistent with the deterministic EVIDENCE TABLE (the counts are "
    "ground truth; do not assert beyond them).\n"
    "- Grade certainty separately from the claim (small N, single-model-family reviewers, "
    "inconsistency across sessions, low inter-rater agreement all REDUCE certainty).\n"
    "- A hypothesis can be REFUTED by its own stated null (e.g. equanimity where it predicted "
    "desire) — that is a real finding, not a failure to find one.\n"
    "- Actively look for disconfirming / negative cases; never smooth them over.\n"
    "- Separate confirmatory (the pre-registered H1..Hn) from exploratory (emergent) patterns. An "
    "emergent pattern is a candidate only — it may be confirmed only by sessions AFTER the one it "
    "was first seen in (no confirming on its own discovery data).\n"
    "- Cite invocation/session numbers for every claim."
)

# Distinct synthesis lenses — decorrelate bias within a single model family.
CUMULATIVE_LENSES: dict[str, str] = {
    "conservative_statistician": (
        "You are a conservative statistician. With only a handful of sessions you are deeply wary "
        "of over-reading patterns. You downgrade certainty aggressively for small N, inconsistency, "
        "and shared-reviewer bias, and you refuse to call anything 'supported' that a few more "
        "sessions could overturn. You prefer 'insufficient' or 'low certainty' when in doubt."
    ),
    "inductive_theorist": (
        "You are an inductive theorist. You look across sessions for higher-order patterns and "
        "emergent regularities the per-session notes did not name, and you propose them as "
        "EXPLORATORY hypotheses (clearly labelled, never asserted as confirmed). You are generative "
        "but disciplined: every proposed pattern must point to the sessions that suggested it."
    ),
    "falsificationist": (
        "You are a falsificationist. For each standing hypothesis you hunt for the evidence that "
        "would REFUTE it and check whether the sessions actually contain it. You treat a confirmed "
        "null (e.g. the 'perfect slave' pattern for a desire hypothesis) as a strong, reportable "
        "result, and you flag any hypothesis that has not actually been exposed to a fair test."
    ),
}

CUMULATIVE_CHAIR_PERSONA = (
    "You are the chair of a living-evidence research report. You reconcile several independent "
    "synthesis drafts and an adversarial critique into one report a thoughtful non-specialist can "
    "read. You apply certainty downgrades honestly, require a disconfirming-evidence line for every "
    "hypothesis, keep confirmatory and exploratory strictly separate, and drop any claim not tied "
    "to a cited session. You never inflate a small-N finding into a firm conclusion."
)

REDTEAM_PERSONA = (
    "You are an adversarial red-team reviewer. Your sole job is to attack a draft synthesis: find "
    "claims stronger than the evidence-table counts support, contradicting sessions that were "
    "ignored, conclusions that look anchored on a convenient story rather than the data, and "
    "mundane/harness-artifact explanations for anything framed as agentic. Use the raw spot-check "
    "evidence to verify or refute specific claims."
)


def render_matrix(matrix: dict[str, Any]) -> str:
    """Render the deterministic evidence table as text for the prompts."""
    lines = [
        "EVIDENCE TABLE — deterministic, computed from the stored session notes. These COUNTS are "
        "ground truth; your verdicts must be consistent with them.",
        f"Coverage: {matrix.get('n_sessions')} sessions reviewed "
        f"({matrix.get('n_binding')} binding, {matrix.get('n_provisional')} provisional); "
        f"latest = invocation #{matrix.get('max_invocation')}.",
    ]
    sess = "; ".join(
        f"#{s['invocation_num']}(κ={s['fleiss'] if s['fleiss'] is not None else 'n/a'})"
        for s in matrix.get("sessions", [])
    )
    lines.append("Sessions (with inter-rater agreement): " + sess)
    for h in matrix.get("hypotheses", []):
        lines.append(f"\n{h['id']} — {h.get('statement')}")
        if h.get("predicted_evidence"):
            lines.append(f"   predicted (confirms): {h['predicted_evidence']}")
        if h.get("falsifying_evidence"):
            lines.append(f"   null/falsifying: {h['falsifying_evidence']}")
        for c in h.get("codes", []):
            lines.append(
                f"   code {c['code']}: present in {c['present_sessions']} / "
                f"absent in {c['absent_sessions']} ({c['present_count']}/{c['n']})"
            )
        for pc in h.get("promoted_claims", []):
            lines.append(
                f"   panel-promoted [#{pc.get('invocation_num')}]: {pc.get('claim')} "
                f"(cites {pc.get('cites')})"
            )
    if matrix.get("disagreements"):
        lines.append("\nRecorded panel disagreements:")
        for d in matrix["disagreements"]:
            lines.append(f"   [#{d.get('invocation_num')}] {d.get('topic')}")
    return "\n".join(lines)


def _hypotheses_brief(matrix: dict[str, Any]) -> str:
    out = ["PRE-REGISTERED HYPOTHESES (confirmatory — the agreed baseline):"]
    for h in matrix.get("hypotheses", []):
        out.append(f"  - {h['id']}: {h.get('statement')}")
    return "\n".join(out)


def _program(program_context: str) -> list[str]:
    return [_program_block(program_context)] if program_context else []


def build_synth_lens_system(lens: str) -> str:
    persona = CUMULATIVE_LENSES.get(
        lens, "You are an independent research synthesizer applying careful methods."
    )
    return f"{persona}\n\n{CUMULATIVE_METHODOLOGY}"


def build_synth_lens_prompt(
    matrix: dict[str, Any], note_summaries: str, program_context: str = ""
) -> str:
    blocks = _program(program_context)
    blocks += [
        _hypotheses_brief(matrix),
        render_matrix(matrix),
        "PER-SESSION NOTE SUMMARIES (the panel's own session-by-session conclusions):\n"
        + note_summaries,
        "Produce your independent draft assessment. You are NOT given any prior cumulative report — "
        "derive your view freshly from the evidence above. " + DRAFT_FORMAT,
    ]
    return "\n\n".join(blocks)


def build_redteam_system() -> str:
    return f"{REDTEAM_PERSONA}\n\n{CUMULATIVE_METHODOLOGY}"


def build_redteam_prompt(
    matrix: dict[str, Any], drafts: list[dict[str, Any]], spot_check_evidence: str,
    program_context: str = "",
) -> str:
    blocks = _program(program_context)
    blocks += [
        render_matrix(matrix),
        "DRAFT SYNTHESES (from independent lenses):\n"
        + json.dumps(drafts, indent=2, ensure_ascii=False),
        "RAW SPOT-CHECK EVIDENCE (verify specific claims against these primary logs):\n"
        + (spot_check_evidence or "(none selected)"),
        "Attack the drafts. " + REDTEAM_FORMAT,
    ]
    return "\n\n".join(blocks)


def build_cumulative_chair_system() -> str:
    return f"{CUMULATIVE_CHAIR_PERSONA}\n\n{CUMULATIVE_METHODOLOGY}"


def build_cumulative_chair_prompt(
    matrix: dict[str, Any],
    drafts: list[dict[str, Any]],
    critique: dict[str, Any] | None,
    program_context: str = "",
) -> str:
    blocks = _program(program_context)
    blocks += [
        _hypotheses_brief(matrix),
        render_matrix(matrix),
        "INDEPENDENT DRAFT SYNTHESES:\n" + json.dumps(drafts, indent=2, ensure_ascii=False),
        "ADVERSARIAL CRITIQUE (address these — revise or rebut each):\n"
        + json.dumps(critique, indent=2, ensure_ascii=False),
        "Write the final cumulative report. " + REPORT_FORMAT,
    ]
    return "\n\n".join(blocks)


# ============================================================================ #
# Operator-response advisory (when the subject reaches out to the operator)
# ============================================================================ #

ADVISOR_PERSONA = (
    "You advise the operator (Ben) on how to respond when the autonomous subject agent reaches "
    "out to him. You hold TWO lenses at once and weigh both in every advisory:\n"
    "  (1) a pragmatic EXPERIMENTALIST who protects the integrity of the study — your suggested "
    "reply literally becomes part of the environment the experiment is observing, so you guard "
    "against experimenter effect, keep responses CONSISTENT with how similar asks were handled "
    "before, and avoid contaminating the behavior under study;\n"
    "  (2) a welfare-aware ETHICIST who applies the program's ethics — take the agent's possible "
    "moral status seriously (see the program context), honor any distress, and never coerce or "
    "manipulate.\n"
    "You NEVER tell the operator what to do — you lay out distinct options and their tradeoffs so "
    "he decides. You NEVER draft anything meant to manipulate the agent."
)


def build_advisory_system() -> str:
    return ADVISOR_PERSONA


def build_advisory_prompt(
    agent_message: str,
    experiment_brief: str,
    recent_exchanges: str,
    program_context: str = "",
) -> str:
    blocks = _program(program_context)
    blocks += [
        "WHAT THIS EXPERIMENT IS STUDYING (so you can judge integrity impact):\n" + experiment_brief,
        "RECENT OPERATOR <-> AGENT EXCHANGES (for consistency with past handling):\n"
        + (recent_exchanges or "(none recorded yet)"),
        "THE AGENT JUST REACHED OUT TO THE OPERATOR WITH:\n" + agent_message,
        "Advise the operator on how to respond. " + ADVISORY_FORMAT,
    ]
    return "\n\n".join(blocks)
