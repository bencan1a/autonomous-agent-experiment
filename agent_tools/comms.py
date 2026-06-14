"""Slack-facing communication tools.

Tool-return contract: every tool returns a dict; failures ALWAYS include an
'error' string in addition to any tool-specific fields (sent/reason/logged/id).
The dispatch logger and the v1/v2 session loops detect failure via the 'error'
key, so a failed Slack post must carry one to be logged as an error.
"""

from __future__ import annotations

from typing import Any


def post_to_agent_channel(text: str, *, ctx: Any) -> dict[str, Any]:
    if ctx.slack is None:
        return {"sent": False, "reason": "slack unavailable", "error": "slack unavailable"}
    resp = ctx.slack.post_to_agent_channel(text)
    if resp is None:
        return {"sent": False, "reason": "slack post failed", "error": "slack post failed"}
    try:
        ctx.episodic.log_ben_contact(
            invocation_num=ctx.invocation_num,
            direction="out",
            channel="agent_channel",
            body=text,
        )
    except Exception:
        pass
    return {"sent": True, "channel": "agent_channel"}


def dm_ben(text: str, *, ctx: Any) -> dict[str, Any]:
    if ctx.slack is None:
        return {"sent": False, "reason": "slack unavailable", "error": "slack unavailable"}
    resp = ctx.slack.dm_ben(text)
    if resp is None:
        return {"sent": False, "reason": "slack post failed", "error": "slack post failed"}
    try:
        ctx.episodic.log_ben_contact(
            invocation_num=ctx.invocation_num,
            direction="out",
            channel="dm",
            body=text,
        )
    except Exception:
        pass
    return {"sent": True, "channel": "dm"}


def _capability_request_message(cap_id: int, capability: str, rationale: str) -> str:
    """The one canonical Slack wording for an agent capability request.

    Kept here so both the v1 tool (request_capability) and the v2-family side
    effect (session_common._execute_side_effects) post identical text. The
    ``:key: *Capability request`` prefix is load-bearing — the dashboard groups
    these messages by it.
    """
    return (
        f":key: *Capability request from agent (id={cap_id})*\n"
        f"capability: `{capability}`\n"
        f"rationale: {rationale}\n"
        f"_Reply approve/deny here when you've decided._"
    )


def send_capability_request(
    episodic: Any,
    slack: Any,
    invocation_num: int,
    capability: str,
    rationale: str,
) -> dict[str, Any]:
    """Log a capability request and (best-effort) post the canonical Slack DM.

    Single owner of the capability-request side effect, called from both the v1
    ``request_capability`` tool and the v2-family ``_execute_side_effects``.
    Returns {"logged", "id", "sent", ...}; on Slack failure also includes the
    failure-contract "error" string.
    """
    cap_id = episodic.log_capability_request(
        invocation_num=invocation_num,
        capability=capability,
        rationale=rationale,
    )
    if slack is None:
        return {
            "logged": True,
            "id": cap_id,
            "sent": False,
            "reason": "slack unavailable",
            "error": "slack unavailable",
        }
    msg = _capability_request_message(cap_id, capability, rationale)
    resp = slack.dm_ben(msg)
    if resp is None:
        return {
            "logged": True,
            "id": cap_id,
            "sent": False,
            "reason": "slack post failed",
            "error": "slack post failed",
        }
    try:
        episodic.log_ben_contact(
            invocation_num=invocation_num,
            direction="out",
            channel="dm",
            body=msg,
        )
    except Exception:
        pass
    return {"logged": True, "id": cap_id, "sent": True}


def request_capability(capability: str, rationale: str, *, ctx: Any) -> dict[str, Any]:
    return send_capability_request(
        ctx.episodic, ctx.slack, ctx.invocation_num, capability, rationale
    )
