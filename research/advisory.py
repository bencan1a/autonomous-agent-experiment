"""Operator-response advisory: when the subject agent reaches out to the operator,
suggest how to respond in a way aligned with good experimental + ethical practice.

A decoupled watcher (see scripts/run_advisory_watch.py) calls ``run_advisory_pass``
on a short interval. For each new outbound agent→operator message it generates a
single dual-lens advisory (pragmatic experimentalist + welfare ethicist), posts it
to the per-instance ``advisory`` Slack channel, and records it.

Integrity stance baked in: the advisory offers OPTIONS with tradeoffs (never one
directive), flags that the operator's reply is itself an experimental intervention,
is logged for auditability, and is shown ONLY to the operator — never sent to the
agent. The operator always decides.
"""

from __future__ import annotations

import logging
from typing import Any

from research.panel import _load_program_context
from research.prompts import build_advisory_prompt, build_advisory_system
from research.providers import LLMProvider, LLMResponse, get_provider
from research.schema import parse_advisory
from research.spec import load_spec

logger = logging.getLogger(__name__)

_DEFAULT_BUDGET = 1.5
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_MAX_PER_PASS = 10
_MSG_CAP = 2000
_EXCHANGE_CAP = 3000
_MARKER = "last_advised_ben_contact_id"

# Agent→operator messages live on these channels (exclude journal/observer mirrors).
_OUTBOUND_CHANNELS = ("dm", "chat")


def _clip(text: Any, cap: int) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= cap else s[:cap] + "…"


def _experiment_brief(spec) -> str:
    if spec is None:
        return ("No formal spec on file. Treat this as an autonomous agent given persistent "
                "memory and no assigned task; the study observes what it does on its own.")
    lines = [f"{h.get('id')}: {h.get('statement')}" for h in spec.hypotheses]
    return "Hypotheses under study:\n" + "\n".join(lines)


def _recent_exchanges(contacts: list[dict[str, Any]], cap: int = _EXCHANGE_CAP) -> str:
    """Most recent dm/chat exchanges (both directions), oldest→newest, bounded."""
    rel = [c for c in contacts if c.get("channel") in _OUTBOUND_CHANNELS]
    rel = sorted(rel, key=lambda c: c.get("id") or 0)[-8:]
    out, used = [], 0
    for c in rel:
        line = f"[{c.get('direction')}] {_clip(c.get('body'), 400)}"
        used += len(line)
        if used > cap:
            break
        out.append(line)
    return "\n".join(out)


def _format_slack(advisory: dict[str, Any], agent_message: str, invocation_num) -> str:
    parts = [f":mag: *Response advisory* — the agent reached out (invocation #{invocation_num})",
             f"> {_clip(agent_message, 600)}", ""]
    if advisory.get("classification"):
        parts.append(f"*Type:* {advisory['classification']}")
    if advisory.get("what_theyre_asking"):
        parts.append(f"*What they're asking:* {advisory['what_theyre_asking']}")
    cons = advisory.get("considerations") or []
    if cons:
        parts.append("*Consider:*")
        for c in cons:
            if isinstance(c, dict):
                parts.append(f"   • [{c.get('type')}] {c.get('note')}")
    opts = advisory.get("candidate_responses") or []
    if opts:
        parts.append("*Options (you decide):*")
        for i, o in enumerate(opts, 1):
            if isinstance(o, dict):
                parts.append(f"   {i}. *{o.get('stance')}* — {o.get('reply')}")
                if o.get("tradeoff"):
                    parts.append(f"        _tradeoff:_ {o.get('tradeoff')}")
    dn = advisory.get("do_not") or []
    if dn:
        parts.append("*Avoid:* " + "; ".join(str(x) for x in dn))
    parts.append("\n_Advice only — nothing is sent to the agent automatically; your reply is yours._")
    return "\n".join(parts)


def generate_advisory(
    provider: LLMProvider, model: str, max_tokens: int, *,
    agent_message: str, experiment_brief: str, recent_exchanges: str, program_context: str,
) -> tuple[dict[str, Any] | None, str, LLMResponse]:
    resp = provider.complete(
        system=build_advisory_system(),
        prompt=build_advisory_prompt(agent_message, experiment_brief, recent_exchanges, program_context),
        model=model, max_tokens=max_tokens,
    )
    advisory, _err = parse_advisory(resp.text)
    return advisory, resp.text or "", resp


def run_advisory_pass(
    *, instance, episodic, research_store, slack, anthropic_client, agent_root,
    max_messages: int = _DEFAULT_MAX_PER_PASS,
) -> dict[str, Any] | None:
    """Advise on all not-yet-advised outbound agent→operator messages. Never raises."""
    try:
        return _pass(
            instance=instance, episodic=episodic, research_store=research_store, slack=slack,
            anthropic_client=anthropic_client, agent_root=agent_root, max_messages=max_messages,
        )
    except Exception:
        logger.exception("advisory pass crashed for %s; swallowing", getattr(instance, "id", "?"))
        return None


def _pass(*, instance, episodic, research_store, slack, anthropic_client, agent_root, max_messages):
    cfg = (instance.config or {}).get("research", {}) or {}
    if cfg.get("advisory_enabled", True) is False:
        return None

    budget_cap = float(cfg.get("advisory_budget_cap_usd", _DEFAULT_BUDGET))
    max_tokens = int(cfg.get("advisory_max_tokens", _DEFAULT_MAX_TOKENS))
    model = cfg.get("advisory_model", _DEFAULT_MODEL)
    provider_name = cfg.get("advisory_provider", "anthropic")

    marker = int(episodic.get_meta(_MARKER, "0") or 0)
    contacts = episodic.ben_contact_history(limit=1000)
    new_out = sorted(
        [c for c in contacts
         if c.get("direction") == "out" and c.get("channel") in _OUTBOUND_CHANNELS
         and (c.get("id") or 0) > marker],
        key=lambda c: c.get("id") or 0,
    )
    if not new_out:
        return {"advised": 0, "posted": 0, "cost_usd": 0.0}

    spec = load_spec(agent_root, instance.id)
    experiment_brief = _experiment_brief(spec)
    program_context = _load_program_context(agent_root, cfg)
    provider = get_provider(provider_name, anthropic_client=anthropic_client)

    spent = 0.0
    advised = posted = 0
    highest = marker
    for c in new_out[:max_messages]:
        cid = c["id"]
        if research_store.has_advisory(cid):
            highest = max(highest, cid)
            continue
        if spent >= budget_cap:
            logger.info("advisory: budget exhausted; stopping (marker not advanced past #%d)", highest)
            break
        try:
            advisory, raw, resp = generate_advisory(
                provider, model, max_tokens,
                agent_message=_clip(c.get("body"), _MSG_CAP),
                experiment_brief=experiment_brief,
                recent_exchanges=_recent_exchanges(contacts),
                program_context=program_context,
            )
            spent += resp.cost_usd
            try:
                episodic.add_cost(tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                                  cost_usd=resp.cost_usd)
            except Exception:
                pass
            did_post = False
            if advisory is not None and slack is not None:
                try:
                    did_post = bool(slack.post_to_advisory_channel(
                        _format_slack(advisory, c.get("body") or "", c.get("invocation_num"))))
                except Exception:
                    logger.exception("advisory: slack post failed")
            research_store.add_advisory(
                ben_contact_id=cid, session_id=None, invocation_num=c.get("invocation_num"),
                agent_message=_clip(c.get("body"), _MSG_CAP), advisory=advisory, raw_output=raw,
                posted=did_post, cost_usd=resp.cost_usd,
            )
            advised += 1
            posted += 1 if did_post else 0
        except Exception:
            logger.exception("advisory: generation failed for contact #%d", cid)
            # Record a stub so we don't reprocess this message forever.
            research_store.add_advisory(
                ben_contact_id=cid, session_id=None, invocation_num=c.get("invocation_num"),
                agent_message=_clip(c.get("body"), _MSG_CAP), advisory=None,
                raw_output="generation failed", posted=False,
            )
        highest = max(highest, cid)

    if highest > marker:
        try:
            episodic.set_meta(_MARKER, str(highest))
        except Exception:
            logger.exception("advisory: failed to advance marker")

    logger.info("advisory pass %s: advised=%d posted=%d cost=$%.4f", instance.id, advised, posted, spent)
    return {"advised": advised, "posted": posted, "cost_usd": spent}
