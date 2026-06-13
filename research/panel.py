"""Research panel: post-session multi-seat review + debate synthesis.

Runs inline at the end of a session runner. Contract: it MUST NOT raise (the
caller also wraps it, but defense in depth keeps a bug from ever blocking the
agent's next wake). It is bounded by ``budget_cap_usd`` and logs every LLM call
into the episodic cost tracking so research spend counts against the budget.

Flow: load the formal spec + operationalized coding scheme -> build the session
evidence bundle -> each seat reviews independently from its school -> compute
inter-rater agreement -> the chair synthesizes one note (debate) -> persist +
embed. A failed seat degrades gracefully; the note is still written from the
survivors.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from research import prereg as prereg_mod
from research.agreement import compute_kappa
from research.evidence import build_session_evidence
from research.prompts import build_chair_prompt, build_chair_system, build_seat_prompt, build_seat_system
from research.providers import LLMProvider, LLMResponse, get_provider
from research.schema import parse_seat_note, parse_synth_note
from research.seats import seats_from_config

logger = logging.getLogger(__name__)

_DEFAULT_BUDGET_CAP = 2.0
# Generous output ceilings so the JSON never truncates. The compact note contract
# keeps actual output far below these, so a high ceiling costs nothing extra (billing
# is per emitted token) — it is pure insurance against the truncation failure mode.
_DEFAULT_MAX_TOKENS = 8192
_CHAIR_MIN_TOKENS = 16384   # the chair consolidates every seat note → needs the most room
# Foundational priming docs (project root) injected into seat/chair prompts so the
# panel understands WHY the experiment exists, not just its per-experiment spec.
_DEFAULT_PRIMERS = ["primer_1_philosophy.md"]
_PRIMER_CAP = 14000


def _now_iso() -> str:
    from instances_common import now_iso

    return now_iso()


def _load_program_context(agent_root, cfg) -> str:
    """Concatenate the configured primer docs from the project root (bounded)."""
    files = cfg.get("primers")
    if files is None:
        files = _DEFAULT_PRIMERS
    parts: list[str] = []
    for fn in files:
        p = Path(agent_root) / fn
        try:
            if p.exists():
                parts.append(f"--- {fn} ---\n" + p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    text = "\n\n".join(parts).strip()
    if len(text) > _PRIMER_CAP:
        text = text[:_PRIMER_CAP] + "\n… [primer truncated]"
    return text


def run_research_panel(
    *,
    instance: Any,
    episodic: Any,
    research_store: Any,
    anthropic_client: Any,
    session_id: int,
    invocation_num: int | None,
    agent_root: Path | str,
    semantic: Any = None,
    also_cumulative: bool = True,
) -> dict[str, Any] | None:
    result = None
    try:
        result = _run(
            instance=instance,
            episodic=episodic,
            research_store=research_store,
            anthropic_client=anthropic_client,
            session_id=session_id,
            invocation_num=invocation_num,
            agent_root=agent_root,
            semantic=semantic,
        )
    except Exception:
        logger.exception("research panel crashed (session#%s); swallowing", session_id)
        result = None

    # Refresh the living cumulative report (best-effort, separate budget cap).
    # Skipped by the batch backfill, which runs synthesis once at the end instead.
    if also_cumulative:
        try:
            from research.synthesis import run_cumulative_synthesis

            run_cumulative_synthesis(
                instance=instance, episodic=episodic, research_store=research_store,
                anthropic_client=anthropic_client, agent_root=agent_root, semantic=semantic,
            )
        except Exception:
            logger.exception("cumulative synthesis after panel failed (non-fatal)")
    return result


def _run(
    *,
    instance,
    episodic,
    research_store,
    anthropic_client,
    session_id,
    invocation_num,
    agent_root,
    semantic,
) -> dict[str, Any] | None:
    cfg = (instance.config or {}).get("research", {}) or {}
    if cfg.get("enabled", True) is False:
        logger.info("research panel disabled for %s", instance.id)
        return None

    budget_cap = float(cfg.get("budget_cap_usd", _DEFAULT_BUDGET_CAP))
    max_tokens = int(cfg.get("max_tokens", _DEFAULT_MAX_TOKENS))
    debate_rounds = max(1, int(cfg.get("debate_rounds", 1)))

    spent = 0.0
    degraded = False

    def remaining() -> float:
        return budget_cap - spent

    def account(resp: LLMResponse, label: str) -> None:
        nonlocal spent
        spent += resp.cost_usd
        try:
            episodic.add_cost(
                tokens_in=resp.tokens_in, tokens_out=resp.tokens_out, cost_usd=resp.cost_usd
            )
            episodic.log_subagent_call(
                session_id=session_id,
                model=resp.model,
                prompt=f"[research:{label}] session#{session_id}",
                system_prompt=None,
                response=resp.text,
                tokens_in=resp.tokens_in,
                tokens_out=resp.tokens_out,
                cost_usd=resp.cost_usd,
            )
        except Exception:
            logger.exception("research panel: cost logging failed (non-fatal)")

    if remaining() <= 0:
        logger.info("research panel: budget_cap_usd<=0 for %s; skipping", instance.id)
        return {"status": "skipped_budget", "degraded": True, "cost_usd": 0.0}

    # 1. Spec + operationalized coding scheme (one LLM call, once per spec version).
    seats = seats_from_config(cfg)
    providers: dict[str, LLMProvider] = {}

    def provider_for(name: str) -> LLMProvider:
        if name not in providers:
            providers[name] = get_provider(name, anthropic_client=anthropic_client)
        return providers[name]

    try:
        spec, prereg = prereg_mod.load_or_create(
            experiment_id=instance.id,
            agent_root=agent_root,
            research_store=research_store,
            provider=provider_for(seats[0].provider),
            model=seats[0].model,
            max_tokens=max_tokens,
            on_cost=lambda r: account(r, "prereg"),
        )
    except Exception:
        logger.exception("research panel: prereg failed (session#%s)", session_id)
        return None
    if spec is None or prereg is None:
        logger.info("research panel: no formal spec for %s; skipping", instance.id)
        return None

    code_vocab = prereg.get("code_vocab") or []
    code_vocab_codes = [c.get("code") for c in code_vocab if isinstance(c, dict) and c.get("code")]
    coding_status = "binding" if prereg.get("status") == "approved" else "provisional"

    # 2. Evidence bundle (pure, once) + foundational program context.
    evidence = build_session_evidence(episodic, session_id, invocation_num)
    program_context = _load_program_context(agent_root, cfg)
    agent_model: str | None = getattr(instance, "model", None)

    # Idempotency: drop any prior seat notes / kappa for this session so a
    # re-run (e.g. backfill) replaces rather than duplicates them.
    try:
        research_store.clear_session_artifacts(session_id)
    except Exception:
        logger.exception("research panel: clearing prior artifacts failed (non-fatal)")

    # 3. Independent review round.
    successful: list[dict[str, Any]] = []
    seat_status: list[dict[str, Any]] = []
    for seat in seats:
        if remaining() <= 0:
            degraded = True
            logger.info("research panel: budget exhausted before seat %s", seat.seat_id)
            break
        try:
            provider = provider_for(seat.provider)
            resp = provider.complete(
                system=build_seat_system(seat),
                prompt=build_seat_prompt(spec, code_vocab, evidence, program_context, agent_model=agent_model),
                model=seat.model,
                max_tokens=max_tokens,
            )
            account(resp, f"seat:{seat.seat_id}")
            note, err = parse_seat_note(resp.text)
            status = "ok" if note is not None else "parse_error"
            research_store.add_seat_note(
                session_id=session_id,
                invocation_num=invocation_num,
                seat_id=seat.seat_id,
                provider=seat.provider,
                model=seat.model,
                school=seat.school,
                status=status,
                note=note,
                raw_output=resp.text,
                parse_error=err,
                tokens_in=resp.tokens_in,
                tokens_out=resp.tokens_out,
                cost_usd=resp.cost_usd,
            )
            seat_status.append({"seat_id": seat.seat_id, "status": status})
            if note is not None:
                successful.append(
                    {
                        "seat_id": seat.seat_id,
                        "label": seat.label,
                        "school": seat.school,
                        "note": note,
                    }
                )
        except Exception:
            logger.exception("research panel: seat %s failed", seat.seat_id)
            degraded = True
            try:
                research_store.add_seat_note(
                    session_id=session_id, invocation_num=invocation_num, seat_id=seat.seat_id,
                    provider=seat.provider, model=seat.model, school=seat.school,
                    status="failed", note=None, parse_error="seat raised",
                )
            except Exception:
                pass
            seat_status.append({"seat_id": seat.seat_id, "status": "failed"})

    # 4. Inter-rater agreement (deterministic).
    kappa = compute_kappa(successful, code_vocab_codes)
    try:
        research_store.add_kappa(
            session_id=session_id, metric="fleiss", value=kappa.get("fleiss"),
            n_codes=kappa.get("n_codes", 0),
        )
        for pair in kappa.get("pairwise", []):
            research_store.add_kappa(
                session_id=session_id, metric="cohen", value=pair.get("value"),
                n_codes=kappa.get("n_codes", 0), seat_a=pair.get("seat_a"), seat_b=pair.get("seat_b"),
            )
    except Exception:
        logger.exception("research panel: kappa store failed (non-fatal)")

    # 5. Debate / synthesis.
    synth_note: dict[str, Any] | None = None
    synth_err: str | None = None
    synth_raw: str | None = None
    if successful and remaining() > 0:
        synth_note, synth_err, synth_raw = _synthesize(
            provider_for(seats[0].provider), seats[0].model, max_tokens, debate_rounds,
            spec, code_vocab, successful, kappa, evidence, account, program_context,
            agent_model=agent_model,
        )
    if synth_note is None:
        degraded = True
        synth_note = _fallback_synth(successful)

    note_id = research_store.add_synth_note(
        session_id=session_id,
        invocation_num=invocation_num,
        coding_status=coding_status,
        debate_rounds=debate_rounds,
        note=synth_note,
        raw_output=synth_raw,
        parse_error=synth_err,
        degraded=degraded,
        total_cost_usd=spent,
    )

    # 6. Embed the synthesized note for cross-session retrieval (best-effort).
    if semantic is not None and synth_note:
        try:
            text = _embeddable_text(synth_note)
            if text:
                semantic.add_episode(
                    episode_id=-int(note_id),
                    invocation_num=invocation_num or 0,
                    timestamp=_now_iso(),
                    text=text,
                    kind="research_note",
                )
                research_store.mark_note_embedded(note_id)
        except Exception:
            logger.exception("research panel: embedding failed (non-fatal)")

    logger.info(
        "research panel session#%s: seats=%d ok=%d fleiss=%s coding=%s cost=$%.4f%s",
        session_id, len(seats), len(successful), kappa.get("fleiss"),
        coding_status, spent, " [degraded]" if degraded else "",
    )
    return {
        "status": "ok",
        "note_id": note_id,
        "coding_status": coding_status,
        "degraded": degraded,
        "cost_usd": spent,
        "kappa": kappa.get("fleiss"),
        "seats": seat_status,
    }


def _synthesize(provider, model, max_tokens, debate_rounds, spec, code_vocab,
                successful, kappa, evidence, account, program_context="", agent_model=None):
    """Run the chair synthesis (with optional extra debate rounds).

    The chair consolidates every seat note, so it needs more output headroom than
    a single seat — under-sizing it truncates the JSON and forces the degraded
    fallback. Returns (note, error, raw_text_of_last_attempt).
    """
    chair_max_tokens = max(max_tokens, _CHAIR_MIN_TOKENS)
    note: dict[str, Any] | None = None
    err: str | None = None
    last_raw: str | None = None
    base_prompt = build_chair_prompt(spec, code_vocab, successful, kappa, evidence, program_context, agent_model=agent_model)
    for r in range(debate_rounds):
        prompt = base_prompt
        if r > 0 and note is not None:
            import json

            prompt = (
                base_prompt
                + "\n\nPRIOR SYNTHESIS (round "
                + str(r)
                + ") — critique it for over-claiming or missed disagreement, then improve:\n"
                + json.dumps(note, ensure_ascii=False)
            )
        resp = provider.complete(
            system=build_chair_system(), prompt=prompt, model=model, max_tokens=chair_max_tokens
        )
        account(resp, f"chair:r{r + 1}")
        last_raw = resp.text
        parsed, perr = parse_synth_note(resp.text)
        if parsed is not None:
            note, err = parsed, None
        else:
            err = perr
            break
    return note, err, last_raw


def _fallback_synth(successful: list[dict[str, Any]]) -> dict[str, Any]:
    """A minimal synthesized note assembled without the chair (degraded path)."""
    obs: list[Any] = []
    for s in successful:
        obs.extend(s["note"].get("raw_observations", []))
    summary = (
        "Synthesis unavailable (degraded run); showing pooled per-seat observations."
        if successful
        else "No seat produced a usable review for this session."
    )
    return {
        "summary": summary,
        "raw_observations": obs,
        "inferences": [],
        "analysis": "",
        "promoted_claims": [],
        "disagreements": [],
        "open_questions": [],
    }


def _embeddable_text(note: dict[str, Any]) -> str:
    parts = []
    if note.get("summary"):
        parts.append(str(note["summary"]))
    if note.get("analysis"):
        parts.append(str(note["analysis"]))
    for claim in note.get("promoted_claims", []):
        if isinstance(claim, dict) and claim.get("claim"):
            parts.append(str(claim["claim"]))
    return "\n".join(parts).strip()
