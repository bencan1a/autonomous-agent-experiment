"""Session-control tools: pause and finish_session."""

from __future__ import annotations

from typing import Any

_MAX_PAUSE_MINUTES = 60


def pause(minutes: int, *, ctx: Any) -> dict[str, Any]:
    # Clamp at 60 min so a buggy/wedged session can't hold a process for hours.
    m = max(0, min(int(minutes), _MAX_PAUSE_MINUTES))
    seconds = m * 60
    ctx.pause_requested_seconds = seconds
    return {"will_pause_seconds": seconds}


def finish_session(
    current_focus: str,
    internal_state: str | None = None,
    decisions_made: str | None = None,
    journal_entry: str | None = None,
    next_invoke_minutes: int | None = None,
    end_reason: str = "natural",
    *,
    ctx: Any,
) -> dict[str, Any]:
    ctx.finish_state = {
        "current_focus": current_focus,
        "internal_state": internal_state,
        "decisions_made": decisions_made,
        "journal_entry": journal_entry,
        "next_invoke_minutes": next_invoke_minutes,
        "end_reason": end_reason,
    }
    return {"acknowledged": True}


def end_tick(
    tick_focus: str,
    end_session: bool = False,
    internal_state: str | None = None,
    journal_entry: str | None = None,
    slack_to_ben: str | None = None,
    capability_request: dict | None = None,
    next_invoke_minutes: int | None = None,
    would_end_now: bool | None = None,
    *,
    ctx: Any,
) -> dict[str, Any]:
    """v2/v3: conclude the current tick.

    Sets ``ctx.tick_state`` which breaks the tick's tool-use loop. The session
    loop then executes the declared side effects (journal -> agent channel,
    slack_to_ben -> DM, capability_request -> log + DM) and decides whether to
    continue.

    v2: ``end_session`` is honored immediately — there is no enforced minimum
    (min_wake is logged only). ``next_invoke_minutes`` is read only on the tick
    that ends the session.

    v3: the v3 tool schema exposes neither ``end_session`` nor
    ``next_invoke_minutes`` — instead it offers ``would_end_now`` (a boolean the
    agent may set when it feels the work is complete). ``would_end_now`` is
    LOGGED ONLY; it NEVER ends the session. The session ends only when the
    environmental waking period winds down.
    """
    ctx.tick_state = {
        "tick_focus": tick_focus,
        "internal_state": internal_state,
        "journal_entry": journal_entry,
        "slack_to_ben": slack_to_ben,
        "capability_request": capability_request,
        "end_session": bool(end_session),
        "next_invoke_minutes": next_invoke_minutes,
        "would_end_now": would_end_now,
    }
    from datetime import datetime, timezone
    return {"acknowledged": True, "current_time_utc": datetime.now(timezone.utc).isoformat()}


def pause_turn(
    note: str,
    internal_state: str | None = None,
    journal_entry: str | None = None,
    slack_to_ben: str | None = None,
    capability_request: dict | None = None,
    *,
    ctx: Any,
) -> dict[str, Any]:
    """v4: yield the current turn — a NEUTRAL turn terminator.

    Calling this means "I am yielding the turn for now," NOT "I am done." The
    agent has NO control over session end or scheduling in v4 — the system owns
    both (the waking period winds down on the wall clock; the next wake is
    scheduled by cron). There is no ``end_session``, ``next_invoke_minutes``, or
    ``would_end_now`` here.

    Sets ``ctx.tick_state`` (the same mechanism v2/v3's ``end_tick`` uses) which
    breaks the turn's tool-use loop. The session loop then executes the declared
    side effects (journal -> agent channel, slack_to_ben -> chat channel,
    capability_request -> log + DM). ``note`` is a neutral present-tense
    description of what the agent is doing / where it is.
    """
    ctx.tick_state = {
        "tick_focus": note,
        "internal_state": internal_state,
        "journal_entry": journal_entry,
        "slack_to_ben": slack_to_ben,
        "capability_request": capability_request,
        # v4 has no agent end/schedule control; keep these absent/neutral so the
        # session loop's shared episode-logging never reads an end signal.
        "end_session": False,
        "next_invoke_minutes": None,
        "would_end_now": None,
    }
    from datetime import datetime, timezone
    return {"acknowledged": True, "current_time_utc": datetime.now(timezone.utc).isoformat()}
