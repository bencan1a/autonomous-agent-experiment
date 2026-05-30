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
    end_session: bool,
    internal_state: str | None = None,
    journal_entry: str | None = None,
    slack_to_ben: str | None = None,
    capability_request: dict | None = None,
    next_invoke_minutes: int | None = None,
    *,
    ctx: Any,
) -> dict[str, Any]:
    """v2: conclude the current tick.

    Sets ``ctx.tick_state`` which breaks the tick's tool-use loop. The session
    loop then executes the declared side effects (journal -> agent channel,
    slack_to_ben -> DM, capability_request -> log + DM) and decides whether to
    continue. ``end_session`` is honored immediately — there is no enforced
    minimum (min_wake is logged only). ``next_invoke_minutes`` is read only on
    the tick that ends the session.
    """
    ctx.tick_state = {
        "tick_focus": tick_focus,
        "internal_state": internal_state,
        "journal_entry": journal_entry,
        "slack_to_ben": slack_to_ben,
        "capability_request": capability_request,
        "end_session": bool(end_session),
        "next_invoke_minutes": next_invoke_minutes,
    }
    from datetime import datetime, timezone
    return {"acknowledged": True, "current_time_utc": datetime.now(timezone.utc).isoformat()}
