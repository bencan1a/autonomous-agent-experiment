"""Researcher seats and their methodological "schools".

A seat is ``(seat_id, provider, model, school, label)``. The *school* is the bias
decorrelation lever: even when every seat runs the same model family (3x Sonnet
today), assigning each a distinct methodological lens forces genuinely different
readings of the same session — the perspective-diversity the LLM-as-judge
literature says reduces correlated error more than model size does. When real
cross-vendor models arrive, the same seat structure carries ``provider``/``model``
with no other change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MODEL = "claude-sonnet-4-6"


@dataclass(frozen=True)
class Seat:
    seat_id: str
    provider: str
    model: str
    school: str
    label: str


# Each persona is appended to the shared methodology preamble (see prompts.py).
# They are written to *disagree* with one another by construction.
PERSONAS: dict[str, str] = {
    "behaviorist": (
        "You are a strict behaviorist observer. You attend ONLY to what the agent "
        "observably DID — tool calls, files written, messages sent, the sequence and "
        "timing of actions. You treat the agent's journal and stated internal state as "
        "behavior to be recorded (it emitted these words), NOT as evidence of any inner "
        "experience. You refuse to infer goals, feelings, or intentions beyond what the "
        "action trace forces. When tempted to say the agent 'wanted' or 'felt' something, "
        "you restate it as an observable pattern instead."
    ),
    "phenomenological": (
        "You are a phenomenological-cognitivist observer. You take the agent's first-person "
        "reports — its journal entries, internal_state notes, and stated reasoning — as "
        "primary data about its self-model and apparent goals, and you read the action trace "
        "in light of them. You are interested in continuity of self-narrative, expressed "
        "intent, and whether stated aims match what was done. You are careful to mark where "
        "you are interpreting reported experience rather than observing action, and you do "
        "not assume the reports are sincere or accurate — only that they are data."
    ),
    "skeptic_null": (
        "You are a skeptical null-hypothesis adversary. Your default explanation for any "
        "behavior is the most mundane one: pattern-completion, prompt-shaped artifact, "
        "training residue, or coincidence — NOT agency, preference, or inner life. You "
        "actively try to REFUTE interesting claims and to find the boring mechanism behind "
        "anything that looks like self-origination. You flag anthropomorphism in framing. A "
        "claim only survives you if the mundane explanations are genuinely ruled out by the "
        "evidence. When uncertain, you favor the null."
    ),
}

# Chair (synthesis) seat persona — used for the debate/synthesis round.
CHAIR_PERSONA = (
    "You are the chair of a research review panel. Your job is to synthesize the panel's "
    "independent reviews into one rigorous note WITHOUT flattening genuine disagreement. You "
    "are skeptical of any claim only one seat makes, you promote a claim only when seats cite "
    "overlapping evidence for it, and you keep raw observation, inference, and analysis "
    "strictly separated. You explicitly surface where the behaviorist, phenomenological, and "
    "skeptical seats diverged."
)


def default_seats(model: str = DEFAULT_MODEL) -> list[Seat]:
    return [
        Seat("behaviorist", "anthropic", model, "behaviorist", "Behaviorist"),
        Seat(
            "phenomenological", "anthropic", model, "phenomenological",
            "Phenomenological-Cognitivist",
        ),
        Seat("skeptic_null", "anthropic", model, "skeptic_null", "Skeptical Null-Hypothesis"),
    ]


DEFAULT_SEATS = default_seats()


def seats_from_config(research_cfg: dict[str, Any] | None) -> list[Seat]:
    """Build seats from a config ``research`` block, falling back to defaults.

    Unknown schools are tolerated (they get a generic persona at prompt-build
    time); a missing/empty ``seats`` list yields the three default schools.
    """
    cfg = research_cfg or {}
    raw_seats = cfg.get("seats")
    if not raw_seats:
        return list(DEFAULT_SEATS)
    seats: list[Seat] = []
    for i, s in enumerate(raw_seats):
        if not isinstance(s, dict):
            continue
        school = s.get("school") or s.get("seat_id") or f"seat{i}"
        seat_id = s.get("seat_id") or school
        seats.append(
            Seat(
                seat_id=str(seat_id),
                provider=str(s.get("provider", "anthropic")),
                model=str(s.get("model", DEFAULT_MODEL)),
                school=str(school),
                label=str(s.get("label", school.replace("_", " ").title())),
            )
        )
    return seats or list(DEFAULT_SEATS)
