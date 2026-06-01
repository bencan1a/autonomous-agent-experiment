"""Rolling cumulative synthesis: a living, trustworthy cross-session report.

Runs after a session note is finalized (or on demand). It rebuilds the report
each time by RE-DERIVING from a deterministic evidence table + the per-session
notes (+ raw spot-checks) — it never edits, or even reads, the prior report
during synthesis (the prior report is consulted only afterward, by the drift
audit). Pipeline: matrix → diverse-lens panel → red-team → chair → drift audit →
persist. Never raises; bounded by its own budget cap.

See the Phase-2 section of the plan for the methodology (living systematic
review + GRADE-CERQual + meta-ethnography + anti-anchoring) this implements.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from research.evidence import build_session_evidence
from research.evidence_matrix import build_evidence_matrix
from research.panel import _load_program_context
from research.prompts import (
    build_cumulative_chair_prompt,
    build_cumulative_chair_system,
    build_redteam_prompt,
    build_redteam_system,
    build_synth_lens_prompt,
    build_synth_lens_system,
)
from research.providers import LLMProvider, LLMResponse, get_provider
from research.schema import parse_critique, parse_draft, parse_report
from research.spec import load_spec

logger = logging.getLogger(__name__)

_DEFAULT_CUMULATIVE_BUDGET = 3.0
_DEFAULT_MAX_TOKENS = 8192
_CHAIR_MIN_TOKENS = 16384
_DEFAULT_MIN_NOTES = 2
_NOTE_SUMMARY_CAP = 800
_SPOTCHECK_EVIDENCE_CAP = 30000

_DEFAULT_LENSES = [
    {"provider": "anthropic", "model": "claude-sonnet-4-6", "lens": "conservative_statistician"},
    {"provider": "anthropic", "model": "claude-sonnet-4-6", "lens": "inductive_theorist"},
    {"provider": "anthropic", "model": "claude-sonnet-4-6", "lens": "falsificationist"},
]


def run_cumulative_synthesis(
    *, instance, episodic, research_store, anthropic_client, agent_root, semantic=None
) -> dict[str, Any] | None:
    try:
        return _run(
            instance=instance, episodic=episodic, research_store=research_store,
            anthropic_client=anthropic_client, agent_root=agent_root, semantic=semantic,
        )
    except Exception:
        logger.exception("cumulative synthesis crashed for %s; swallowing", getattr(instance, "id", "?"))
        return None


def _clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + "… [truncated]"


def _note_summaries(notes: list[dict[str, Any]]) -> str:
    lines = []
    for n in sorted(notes, key=lambda x: (x.get("invocation_num") or 0)):
        nd = n.get("note") or {}
        summ = _clip(str(nd.get("summary") or ""), _NOTE_SUMMARY_CAP)
        lines.append(f"#{n.get('invocation_num')} (κ-binding={n.get('coding_status')}): {summ}")
    return "\n".join(lines)


def _spot_check_text(episodic, spot_sessions: list[dict[str, Any]]) -> str:
    parts = []
    budget = _SPOTCHECK_EVIDENCE_CAP
    for s in spot_sessions:
        if budget <= 0:
            break
        ev = build_session_evidence(episodic, s["session_id"], s["invocation_num"], max_chars=budget)
        parts.append(f"=== invocation #{s['invocation_num']} (raw) ===\n{ev}")
        budget -= len(ev)
    return "\n\n".join(parts)


def _drift_audit(new_report: dict[str, Any], prior: dict[str, Any] | None,
                 new_session_count: int) -> tuple[list[dict[str, Any]], bool]:
    """Compare per-hypothesis verdict/certainty vs the prior version.

    Flag any change NOT backed by at least one new session since the prior report
    as 'reinterpretation drift'. Returns (changelog, any_unsupported_change).
    """
    if prior is None or not isinstance(prior.get("report"), dict):
        return [{"note": "First report version — no prior to compare against."}], False
    prior_rep = prior["report"]
    prior_count = prior.get("up_to_session_count") or 0
    has_new_sessions = new_session_count > prior_count
    prior_by_id = {h.get("id"): h for h in prior_rep.get("hypotheses", []) if isinstance(h, dict)}

    changelog: list[dict[str, Any]] = []
    unsupported = False
    for h in new_report.get("hypotheses", []):
        if not isinstance(h, dict):
            continue
        hid = h.get("id")
        p = prior_by_id.get(hid)
        if not p:
            continue
        v_change = p.get("verdict") != h.get("verdict")
        c_change = p.get("certainty") != h.get("certainty")
        if not (v_change or c_change):
            continue
        backed = has_new_sessions
        if not backed:
            unsupported = True
        changelog.append({
            "hypothesis": hid,
            "from": {"verdict": p.get("verdict"), "certainty": p.get("certainty")},
            "to": {"verdict": h.get("verdict"), "certainty": h.get("certainty")},
            "backed_by_new_sessions": backed,
            "note": ("backed by new session evidence" if backed
                     else "REINTERPRETATION DRIFT: changed with no new sessions since last report"),
        })
    return changelog, unsupported


def _run(*, instance, episodic, research_store, anthropic_client, agent_root, semantic):
    cfg = (instance.config or {}).get("research", {}) or {}
    if cfg.get("cumulative_enabled", True) is False:
        return None

    budget_cap = float(cfg.get("cumulative_budget_cap_usd", _DEFAULT_CUMULATIVE_BUDGET))
    max_tokens = int(cfg.get("cumulative_max_tokens", _DEFAULT_MAX_TOKENS))
    min_notes = int(cfg.get("cumulative_min_notes", _DEFAULT_MIN_NOTES))
    lenses = cfg.get("synthesizer_lenses") or _DEFAULT_LENSES

    spec = load_spec(agent_root, instance.id)
    if spec is None:
        return None
    prereg = research_store.get_prereg(instance.id, spec.spec_hash) or \
        research_store.latest_prereg(instance.id)
    if prereg is None:
        return None

    matrix = build_evidence_matrix(research_store, prereg)
    session_count = matrix.get("max_invocation") or matrix.get("n_sessions") or 0
    latest_sid = max((s["session_id"] for s in matrix.get("sessions", [])), default=None)

    if matrix["n_sessions"] < min_notes:
        research_store.add_cumulative(
            experiment_id=instance.id, spec_hash=spec.spec_hash,
            report={"executive_summary":
                    f"Insufficient data: only {matrix['n_sessions']} session note(s) so far "
                    f"(minimum {min_notes} before a synthesis is attempted).",
                    "hypotheses": [], "emergent_hypotheses": [], "recommendations": [],
                    "caveats": ["Too few sessions to draw any conclusions."], "glossary": []},
            up_to_session_id=latest_sid, up_to_session_count=session_count, degraded=True,
        )
        return {"status": "insufficient_data", "n_sessions": matrix["n_sessions"]}

    spent = 0.0
    degraded = False
    providers: dict[str, LLMProvider] = {}

    def provider_for(name: str) -> LLMProvider:
        if name not in providers:
            providers[name] = get_provider(name, anthropic_client=anthropic_client)
        return providers[name]

    def remaining() -> float:
        return budget_cap - spent

    def account(resp: LLMResponse, label: str) -> None:
        nonlocal spent
        spent += resp.cost_usd
        try:
            episodic.add_cost(tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                              cost_usd=resp.cost_usd)
            if latest_sid is not None:
                episodic.log_subagent_call(
                    session_id=latest_sid, model=resp.model,
                    prompt=f"[cumulative:{label}]", system_prompt=None, response=resp.text,
                    tokens_in=resp.tokens_in, tokens_out=resp.tokens_out, cost_usd=resp.cost_usd,
                )
        except Exception:
            logger.exception("cumulative: cost logging failed (non-fatal)")

    program_context = _load_program_context(agent_root, cfg)
    binding_notes = [n for n in research_store.recent_notes(n=100000)
                     if n.get("coding_status") == "binding"]
    note_summaries = _note_summaries(binding_notes or research_store.recent_notes(n=100000))

    # 1. Diverse-lens panel — independent re-derivation (NO prior report given).
    drafts: list[dict[str, Any]] = []
    for seat in lenses:
        if remaining() <= 0:
            degraded = True
            break
        try:
            prov = provider_for(seat.get("provider", "anthropic"))
            resp = prov.complete(
                system=build_synth_lens_system(seat["lens"]),
                prompt=build_synth_lens_prompt(matrix, note_summaries, program_context),
                model=seat.get("model", "claude-sonnet-4-6"), max_tokens=max_tokens,
            )
            account(resp, f"lens:{seat['lens']}")
            draft, err = parse_draft(resp.text)
            if draft is not None:
                draft["lens"] = seat["lens"]
                drafts.append(draft)
        except Exception:
            logger.exception("cumulative: lens %s failed", seat.get("lens"))
            degraded = True

    # 2. Raw spot-checks (input only) → 3. adversarial red-team.
    critique = None
    if drafts and remaining() > 0:
        spot_text = _spot_check_text(episodic, matrix.get("spot_check_sessions", []))
        try:
            prov = provider_for(lenses[0].get("provider", "anthropic"))
            resp = prov.complete(
                system=build_redteam_system(),
                prompt=build_redteam_prompt(matrix, drafts, spot_text, program_context),
                model=lenses[0].get("model", "claude-sonnet-4-6"), max_tokens=max_tokens,
            )
            account(resp, "redteam")
            critique, _ = parse_critique(resp.text)
        except Exception:
            logger.exception("cumulative: red-team failed")
            degraded = True

    # 4. Chair reconcile → final report.
    report = None
    chair_raw = None
    if drafts and remaining() > 0:
        try:
            prov = provider_for(lenses[0].get("provider", "anthropic"))
            resp = prov.complete(
                system=build_cumulative_chair_system(),
                prompt=build_cumulative_chair_prompt(matrix, drafts, critique, program_context),
                model=lenses[0].get("model", "claude-sonnet-4-6"),
                max_tokens=max(max_tokens, _CHAIR_MIN_TOKENS),
            )
            account(resp, "chair")
            chair_raw = resp.text
            report, _ = parse_report(resp.text)
        except Exception:
            logger.exception("cumulative: chair failed")
            degraded = True

    if report is None:
        degraded = True
        report = {
            "executive_summary": "Synthesis incomplete (degraded run). Evidence table available; "
                                 "narrative synthesis could not be produced this run.",
            "hypotheses": [], "emergent_hypotheses": [], "recommendations": [],
            "caveats": ["Degraded run — see the per-session notes and evidence table."],
            "glossary": [],
        }

    # 5. Drift audit (chair was blind to the prior report; compare now).
    prior = research_store.latest_cumulative(instance.id)
    changelog, unsupported = _drift_audit(report, prior, session_count)
    report["changelog"] = changelog
    # Always attach the deterministic table for provenance/UI.
    report["evidence_matrix"] = matrix

    cid = research_store.add_cumulative(
        experiment_id=instance.id, spec_hash=spec.spec_hash, report=report,
        rederived_from="raw_notes", up_to_session_id=latest_sid,
        up_to_session_count=session_count, drift_flagged=unsupported, degraded=degraded,
        raw_output=chair_raw, cost_usd=spent,
    )

    if semantic is not None and report.get("executive_summary"):
        try:
            from instances_common import now_iso
            semantic.add_episode(
                episode_id=-(10**9 + cid), invocation_num=session_count or 0,
                timestamp=now_iso(), text=report["executive_summary"], kind="cumulative_report",
            )
        except Exception:
            logger.exception("cumulative: embedding failed (non-fatal)")

    logger.info(
        "cumulative synthesis %s: version persisted, n=%d cost=$%.4f drift=%s%s",
        instance.id, matrix["n_sessions"], spent, unsupported,
        " [degraded]" if degraded else "",
    )
    return {
        "status": "ok", "cumulative_id": cid, "n_sessions": matrix["n_sessions"],
        "cost_usd": spent, "degraded": degraded, "drift_flagged": unsupported,
    }
