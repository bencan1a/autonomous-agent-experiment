"""Pre-registration: operationalize the agreed spec into a coding scheme.

This is NOT goal invention — the hypotheses/variables/criteria are copied verbatim
from the human-authored spec. The one LLM call here only turns the spec into an
operational behavior-coding scheme (the shared vocabulary the seats apply and kappa
is measured over). It runs once per spec version; the operator then approves the
scheme once before notes become ``binding``.

Editing the spec changes its ``spec_hash``, which yields a new (unapproved) prereg
row — the drift-detection seam.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from claude_client import _extract_json
from research.prompts import build_prereg_prompt, build_prereg_system
from research.providers import LLMProvider, LLMResponse
from research.spec import Spec, load_spec

logger = logging.getLogger(__name__)


def _operationalize(provider: LLMProvider, model: str, max_tokens: int, spec: Spec,
                    on_cost: Callable[[LLMResponse], None] | None) -> tuple[list[dict], str]:
    """One LLM call: spec -> code_vocab. Falls back to spec candidates on failure."""
    resp = provider.complete(
        system=build_prereg_system(),
        prompt=build_prereg_prompt(spec),
        model=model,
        max_tokens=max_tokens,
    )
    if on_cost is not None:
        on_cost(resp)
    parsed, err = _extract_json(resp.text or "")
    code_vocab: list[dict] = []
    if isinstance(parsed, dict) and isinstance(parsed.get("code_vocab"), list):
        for c in parsed["code_vocab"]:
            if isinstance(c, dict) and c.get("code"):
                code_vocab.append(
                    {
                        "code": str(c.get("code")),
                        "definition": str(c.get("definition", "")),
                        "maps_to_hypothesis": c.get("maps_to_hypothesis"),
                    }
                )
    if not code_vocab:
        logger.warning("prereg: could not parse code_vocab (%s); using spec candidates", err)
        code_vocab = [
            {"code": c.get("code"), "definition": c.get("definition", ""), "maps_to_hypothesis": None}
            for c in spec.candidate_behavior_codes
            if isinstance(c, dict) and c.get("code")
        ]
    return code_vocab, resp.text or ""


def load_or_create(
    *,
    experiment_id: str,
    agent_root: Path | str,
    research_store: Any,
    provider: LLMProvider,
    model: str,
    max_tokens: int = 2048,
    on_cost: Callable[[LLMResponse], None] | None = None,
) -> tuple[Spec | None, dict[str, Any] | None]:
    """Return (spec, prereg_row) for the experiment, operationalizing once if needed.

    Returns (None, None) when there is no valid formal spec — the panel then skips.
    The prereg row carries ``status`` (pending_approval | approved) and the parsed
    ``code_vocab``.
    """
    spec = load_spec(agent_root, experiment_id)
    if spec is None:
        return None, None

    existing = research_store.get_prereg(experiment_id, spec.spec_hash)
    if existing is not None:
        # Already operationalized for this spec version — don't re-call the LLM,
        # regardless of approval status.
        return spec, existing

    code_vocab, raw = _operationalize(provider, model, max_tokens, spec, on_cost)
    research_store.add_prereg(
        experiment_id=experiment_id,
        spec_hash=spec.spec_hash,
        spec_source=str(Path(agent_root) / "experiments" / f"{experiment_id}.md"),
        hypotheses=spec.hypotheses,
        ivars=spec.independent_variables,
        dvars=spec.dependent_variables,
        controls=spec.controls_held_constant,
        success=spec.success_criteria,
        stopping=spec.stopping_conditions,
        code_vocab=code_vocab,
        raw_output=raw,
        model=model,
    )
    return spec, research_store.get_prereg(experiment_id, spec.spec_hash)
