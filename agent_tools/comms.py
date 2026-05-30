"""Slack-facing communication tools."""

from __future__ import annotations

from typing import Any


def post_to_agent_channel(text: str, *, ctx: Any) -> dict[str, Any]:
    if ctx.slack is None:
        return {"sent": False, "reason": "slack unavailable"}
    resp = ctx.slack.post_to_agent_channel(text)
    if resp is None:
        return {"sent": False, "reason": "slack post failed"}
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
        return {"sent": False, "reason": "slack unavailable"}
    resp = ctx.slack.dm_ben(text)
    if resp is None:
        return {"sent": False, "reason": "slack post failed"}
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


def request_capability(capability: str, rationale: str, *, ctx: Any) -> dict[str, Any]:
    cap_id = ctx.episodic.log_capability_request(
        invocation_num=ctx.invocation_num,
        capability=capability,
        rationale=rationale,
    )
    if ctx.slack is None:
        return {
            "logged": True,
            "id": cap_id,
            "sent": False,
            "reason": "slack unavailable",
        }
    msg = (
        f":key: *Capability request from agent (id={cap_id})*\n"
        f"capability: `{capability}`\n"
        f"rationale: {rationale}\n"
        f"_Reply approve/deny here when you've decided._"
    )
    resp = ctx.slack.dm_ben(msg)
    if resp is None:
        return {"logged": True, "id": cap_id, "sent": False, "reason": "slack post failed"}
    try:
        ctx.episodic.log_ben_contact(
            invocation_num=ctx.invocation_num,
            direction="out",
            channel="dm",
            body=msg,
        )
    except Exception:
        pass
    return {"logged": True, "id": cap_id, "sent": True}
