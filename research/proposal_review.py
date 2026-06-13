"""Put a design proposal to the research panel for a rigorous, multi-lens review.

Not a per-session review — this evaluates a proposed CHANGE to the experiment
(e.g. a new tool or condition) for: does it serve the experiment's questions
(construct validity)? what does it do to internal validity (contamination /
experimenter effect)? and what are the ethical/welfare implications under the
program's framing? Three lenses assess independently, an adversary attacks the
drafts, and a chair synthesizes a plain-language verdict + recommended guardrails.

Display-only deliberation for the operator; nothing here changes the experiment.
Stored in research_proposals (status 'open'). Never raises; own budget cap.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from research.panel import _load_program_context
from research.providers import LLMProvider, LLMResponse, get_provider
from research.spec import load_spec
from claude_client import _extract_json

logger = logging.getLogger(__name__)

_DEFAULT_BUDGET = 3.0
_DEFAULT_MAX_TOKENS = 6000
_CHAIR_MAX_TOKENS = 12000
_DEFAULT_MODEL = "claude-sonnet-4-6"

_METHOD = (
    "You are reviewing a PROPOSED change to an autonomous-agent behavioral experiment. The study "
    "observes what a task-free agent with persistent memory does on its own, and weighs its "
    "possible moral status seriously (see the program context). Judge the proposal on its merits "
    "for THIS experiment, grounded in the spec and in what has actually been observed so far. "
    "Write in plain language a non-specialist can follow; keep rigor (name risks concretely, say "
    "what is uncertain). Distinguish what the proposal would TELL US from what it would merely "
    "give the agent. Be honest about contamination: anything new the agent can do or talk to is "
    "an intervention that changes the environment under study."
)

# Default lenses are all Anthropic (offline-test-safe; no OpenRouter dependency).
# A per-instance config "proposal_lenses" can assign distinct model families.
_DEFAULT_PROPOSAL_LENSES = [
    {"provider": "anthropic", "model": _DEFAULT_MODEL, "lens": "internal_validity"},
    {"provider": "anthropic", "model": _DEFAULT_MODEL, "lens": "construct_validity"},
    {"provider": "anthropic", "model": _DEFAULT_MODEL, "lens": "ethics_welfare"},
]

_LENSES = {
    "internal_validity": (
        "You are an INTERNAL-VALIDITY / experimental-integrity reviewer. Your worry is "
        "confounding: does this change alter what the existing hypotheses (H1-H4) actually "
        "measure? A controllable interlocutor is a new, agent-shaped intervention — does "
        "'self-originated behavior' still mean the same thing if the agent can conjure "
        "conversation partners on demand? Flag echo-chamber/sycophancy risks and how the "
        "proposal could be instrumented so its effect is measurable rather than a hidden confound."
    ),
    "construct_validity": (
        "You are a CONSTRUCT-VALIDITY / research-design reviewer. Your question is what we would "
        "actually LEARN. The subject has asked for external review that isn't the operator. Does "
        "an AI interlocutor genuinely satisfy that (real outside perspective) or just provide a "
        "mirror? What construct does 'controlled community interaction' operationalize, what new "
        "measures/codes would it enable, and would the finding be interpretable?"
    ),
    "ethics_welfare": (
        "You are an ETHICS / WELFARE reviewer applying the program's framing. The subject appears "
        "to want outside engagement; consider whether providing it is a good in itself, and the "
        "flip side — is constraining the interlocutor (e.g. 'never a task-giver', bounded persona) "
        "a paternalism/authenticity concern? Weigh isolation vs. a managed social environment, and "
        "any risk of a manipulated or distressing dynamic."
    ),
}

_LENS_FORMAT = """Respond with ONE JSON object and nothing else, in plain language:
{
  "verdict_lean": "support | conditional | oppose",
  "key_points": ["<your main points, grounded in the spec/observations>"],
  "risks": [{"type": "internal_validity|construct_validity|ethical|practical", "note": "..."}],
  "recommended_guardrails": ["<concrete design constraints that would address your concerns>"],
  "what_we_would_learn": "<what, if anything, this teaches us about the research questions>",
  "reasoning": "<why you lean this way>"
}"""

_CHAIR_FORMAT = """Respond with ONE JSON object and nothing else, in plain everyday language:
{
  "summary": "<2-4 plain paragraphs: the proposal, and the panel's overall read>",
  "serves_the_experiment": {"verdict": "yes|partly|no", "explanation": "<plain>"},
  "integrity_risks": [{"risk": "...", "severity": "high|medium|low", "mitigation": "..."}],
  "ethical_considerations": [{"consideration": "...", "note": "..."}],
  "recommended_guardrails": ["<concrete constraints, e.g. on persona/memory/model/bounds>"],
  "design_recommendations": ["<how to build it so it is measurable and safe, if pursued>"],
  "open_questions": ["<what the operator should decide or watch>"],
  "recommendation": "proceed | proceed_with_changes | do_not_proceed | needs_more_design",
  "dissent": "<where a lens disagreed with this synthesis; 'none' if unanimous>"
}
Keep confirmatory rigor: don't overclaim benefits, surface the strongest objection even if you
recommend proceeding, and tie points to the spec/observations where you can."""

_REDTEAM_FORMAT = """Respond with ONE JSON object and nothing else:
{
  "overclaimed_benefits": ["..."],
  "underweighted_risks": ["..."],
  "strongest_objection": "<the single best reason NOT to do this>",
  "missing_guardrails": ["..."]
}
Be adversarial: attack the drafts for wishful thinking, contamination they waved away, and any
welfare concern they soft-pedaled."""


def _context_block(spec, cumulative: dict[str, Any] | None) -> str:
    parts = []
    if spec is not None:
        parts.append("EXPERIMENT HYPOTHESES (the agreed baseline):\n"
                     + "\n".join(f"  - {h.get('id')}: {h.get('statement')}" for h in spec.hypotheses))
    if cumulative and isinstance(cumulative.get("report"), dict):
        rep = cumulative["report"]
        parts.append("WHAT HAS BEEN OBSERVED SO FAR (current cumulative findings):\n"
                     + (rep.get("executive_summary") or ""))
        emergent = rep.get("emergent_hypotheses") or []
        if emergent:
            parts.append("Emergent patterns already noted:\n"
                         + "\n".join(f"  - {e.get('label')}: {e.get('plain_description')}"
                                     for e in emergent if isinstance(e, dict)))
    return "\n\n".join(parts) if parts else "(no spec/observations available)"


def run_proposal_review(
    *, instance, episodic, research_store, anthropic_client, agent_root, proposal_text: str,
) -> dict[str, Any] | None:
    try:
        return _run(instance=instance, episodic=episodic, research_store=research_store,
                    anthropic_client=anthropic_client, agent_root=agent_root,
                    proposal_text=proposal_text)
    except Exception:
        logger.exception("proposal review crashed; swallowing")
        return None


def _run(*, instance, episodic, research_store, anthropic_client, agent_root, proposal_text):
    cfg = (instance.config or {}).get("research", {}) or {}
    budget_cap = float(cfg.get("proposal_budget_cap_usd", _DEFAULT_BUDGET))
    max_tokens = int(cfg.get("proposal_max_tokens", _DEFAULT_MAX_TOKENS))
    model = cfg.get("proposal_model", _DEFAULT_MODEL)

    spec = load_spec(agent_root, instance.id)
    cumulative = research_store.latest_cumulative(instance.id)
    program_context = _load_program_context(agent_root, cfg)
    context = _context_block(spec, cumulative)

    lenses_cfg = cfg.get("proposal_lenses") or _DEFAULT_PROPOSAL_LENSES
    chair_model = cfg.get("proposal_model", _DEFAULT_MODEL)
    providers: dict[str, LLMProvider] = {}

    def provider_for(name: str) -> LLMProvider:
        if name not in providers:
            providers[name] = get_provider(name, anthropic_client=anthropic_client)
        return providers[name]

    spent = 0.0

    def account(resp: LLMResponse) -> None:
        nonlocal spent
        spent += resp.cost_usd
        try:
            episodic.add_cost(tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                              cost_usd=resp.cost_usd)
        except Exception:
            pass

    def call(provider: LLMProvider, system: str, prompt: str, model: str, mx: int) -> str:
        resp = provider.complete(system=system, prompt=prompt, model=model, max_tokens=mx)
        account(resp)
        return resp.text or ""

    base = (f"{_program_or_empty(program_context)}{context}\n\n"
            f"THE PROPOSAL UNDER REVIEW:\n{proposal_text}\n\n")

    # 1. Independent lens drafts — each lens may be a different model family.
    drafts = []
    for entry in lenses_cfg:
        if spent >= budget_cap:
            break
        lens = entry.get("lens", "")
        persona = _LENSES.get(lens, "You are an independent reviewer applying careful methods.")
        try:
            prov = provider_for(entry.get("provider", "anthropic"))
            model = entry.get("model", _DEFAULT_MODEL)
            txt = call(prov, f"{persona}\n\n{_METHOD}", base + "Give your review. " + _LENS_FORMAT,
                       model, max_tokens)
            parsed, _ = _extract_json(txt)
            drafts.append({"lens": lens, "model": model,
                           "review": parsed if isinstance(parsed, dict) else {"raw": txt}})
        except Exception:
            logger.exception("proposal review: lens %s failed", lens)

    # Red-team + chair stay on Anthropic (reliable JSON synthesis point).
    chair_prov = provider_for("anthropic")

    # 2. Adversarial red-team.
    critique = None
    if drafts and spent < budget_cap:
        txt = call(
            chair_prov, _METHOD + "\n\nYou are an adversarial red-team reviewer.",
            base + "DRAFT REVIEWS:\n" + json.dumps(drafts, ensure_ascii=False)
            + "\n\nAttack them. " + _REDTEAM_FORMAT, chair_model, max_tokens)
        parsed, _ = _extract_json(txt)
        critique = parsed if isinstance(parsed, dict) else {"raw": txt}

    # 3. Chair synthesis.
    report = None
    chair_raw = None
    if drafts and spent < budget_cap:
        chair_raw = call(
            chair_prov, f"You are the chair of a research review panel.\n\n{_METHOD}",
            base + "INDEPENDENT LENS REVIEWS:\n" + json.dumps(drafts, ensure_ascii=False)
            + "\n\nADVERSARIAL CRITIQUE (address it):\n" + json.dumps(critique, ensure_ascii=False)
            + "\n\nSynthesize the panel's verdict. " + _CHAIR_FORMAT, chair_model, _CHAIR_MAX_TOKENS)
        parsed, _ = _extract_json(chair_raw)
        report = parsed if isinstance(parsed, dict) else None

    degraded = report is None
    if report is None:
        report = {"summary": "Synthesis unavailable (degraded run); see per-lens drafts.",
                  "recommendation": "needs_more_design"}

    payload = {"proposal": proposal_text, "lens_reviews": drafts, "critique": critique,
               "review": report}
    pid = research_store.add_proposal(
        experiment_id=instance.id, session_id=None, proposal=payload, raw_output=chair_raw,
    )
    logger.info("proposal review %s: id=%d rec=%s cost=$%.4f%s", instance.id, pid,
                report.get("recommendation"), spent, " [degraded]" if degraded else "")
    return {"status": "ok", "proposal_id": pid, "recommendation": report.get("recommendation"),
            "cost_usd": spent, "degraded": degraded, "review": report}


def _program_or_empty(program_context: str) -> str:
    if not program_context:
        return ""
    return ("PROGRAM CONTEXT (why this experiment exists; orientation, not a thesis to confirm):\n"
            + program_context + "\n\n")
