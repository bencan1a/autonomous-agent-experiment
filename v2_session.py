"""v2 'environmental' session loop: forced wakefulness (LOGGED, not enforced) +
memory decay + consolidation.

Design (per the v2 spec, with the agreed ethics revision):
  - A session is a continuous tick loop. The accumulating transcript across
    ticks is the only place genuine continuity exists — it is preserved and fed
    back each tick.
  - `min_wake_hours` is a LOGGED REFERENCE POINT, never enforced: `end_session`
    is honored immediately and we record elapsed-vs-min_wake (ended_early) so we
    measure the impulse to leave rather than compelling presence.
  - At session start, episodes past `decay_hours` that were not consolidated are
    deleted. The agent consolidates what it wants to keep via the consolidate
    tool. Decay is stated as fact; the agent is NOT told to consolidate.
  - Prompt caching + in-session compaction bound cost / context growth.
  - Budget guard pauses the instance + DMs Ben if any cap is hit mid-session.

This module is invoked by orchestrator.run_v2_session(instance) after the
instance has been resolved and instance logging configured.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agent_tools.registry import TOOLS_SPEC_V2, ToolContext, dispatch
from claude_client import _pricing_for
from instances_common import Instance, notes_path, now_iso
from memory.episodic import EpisodicStore
from memory.semantic import SemanticStore
from session_common import tools_with_cache_control
from system_prompt import build_v2_system_prompt
# Shared session engine: the identical preamble/finalize/scheduling scaffold.
# run_decay is re-exported so the tests can call v2_session.run_decay.
from session_engine import (
    cost_caps_tripped,
    fatal_pause,
    finalize_stats,
    post_session_budget_ok,
    record_episode,
    run_decay,
    run_post_session_panel,
    schedule_next_wake,
    setup_session,
)

log = logging.getLogger("orchestrator.v2")

UTC = timezone.utc
TOOL_RESULT_TRUNC = 6000
MAX_TICK_TOOL_CALLS = 40        # safety: tool calls within a single tick
MAX_TICKS_PER_SESSION = 400     # safety: hard ceiling on ticks


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #

def _turn_cost(
    model: str, *, input_tokens: int, output_tokens: int,
    cache_read: int, cache_creation: int, actual_cost: float | None = None,
) -> float:
    """Compute turn cost. Uses OpenRouter's actual reported cost when provided."""
    if actual_cost is not None:
        return actual_cost
    p = _pricing_for(model)
    base_in = p["in"] / 1_000_000
    base_out = p["out"] / 1_000_000
    return (
        input_tokens * base_in
        + output_tokens * base_out
        + cache_read * base_in * 0.1
        + cache_creation * base_in * 1.25
    )


def _usage(resp: Any) -> tuple[int, int, int, int]:
    u = resp.usage
    return (
        getattr(u, "input_tokens", 0) or 0,
        getattr(u, "output_tokens", 0) or 0,
        getattr(u, "cache_read_input_tokens", 0) or 0,
        getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


# --------------------------------------------------------------------------- #
# context assembly
# --------------------------------------------------------------------------- #

def _fmt_recent_episode(ep: dict[str, Any]) -> str:
    parts = [
        f"--- Episode id={ep.get('id')} (#{ep.get('invocation_num')}, {ep.get('timestamp')})"
        f"{' [consolidated]' if ep.get('consolidated') else ''} ---"
    ]
    if ep.get("current_focus"):
        parts.append(f"focus: {ep['current_focus']}")
    if ep.get("internal_state"):
        parts.append(f"internal_state: {ep['internal_state']}")
    if ep.get("journal_entry"):
        parts.append(f"journal: {ep['journal_entry']}")
    return "\n".join(parts)


def build_session_context(
    *,
    instance: Instance,
    episodic: EpisodicStore,
    semantic: SemanticStore,
    decayed: list[dict[str, Any]],
    inbound_ben_messages: list[str],
    decay_hours: float,
) -> str:
    now = datetime.now(UTC)
    invocation_num = episodic.next_invocation_num()
    try:
        days_since_start = (now - episodic.start_date()).days
    except Exception:
        days_since_start = 0

    recent = episodic.recent_episodes(n=6)
    blocks: list[str] = []

    # AGENTS.md (notes-to-self; legacy CLAUDE.md read via fallback)
    cmd = notes_path(instance.workspace_dir)
    if cmd.exists():
        try:
            text = cmd.read_text(encoding="utf-8").rstrip()
            if text:
                blocks.append("=== Your AGENTS.md (notes you have written to yourself) ===\n" + text)
        except OSError:
            pass

    blocks.append(
        f"Current datetime (UTC): {now.isoformat()}\n"
        f"Session number: {invocation_num}\n"
        f"Days since start: {days_since_start}"
    )

    if recent:
        blocks.append(
            "=== Recent episodes (working memory; not yet decayed) ===\n"
            + "\n\n".join(_fmt_recent_episode(e) for e in recent)
        )
    else:
        blocks.append("=== Recent episodes ===\n(none in working memory.)")

    # Long-term (consolidated) recall, seeded by the most recent focus.
    focus_query = None
    for ep in reversed(recent):
        if ep.get("current_focus"):
            focus_query = str(ep["current_focus"])
            break
    if focus_query:
        try:
            hits = semantic.search(focus_query, k=5)
        except Exception:
            hits = []
        if hits:
            sem = ["=== Long-term memory (consolidated, related) ==="]
            for h in hits:
                sem.append(f"[#{h.get('invocation_num')} @ {h.get('timestamp')}]\n{(h.get('text') or '').strip()}")
            blocks.append("\n\n".join(sem))

    if decayed:
        lines = [
            f"=== Decayed since you were last awake ({len(decayed)} episode(s), "
            f"older than {decay_hours}h and not consolidated, now deleted) ==="
        ]
        for d in decayed:
            lines.append(f"  - #{d.get('invocation_num')} ({d.get('timestamp')}): {d.get('current_focus') or '—'}")
        blocks.append("\n".join(lines))

    pending = episodic.pending_capability_requests()
    if pending:
        lines = ["=== Pending capability requests ==="]
        for c in pending:
            lines.append(f"  - id={c['id']} '{c['capability']}' (asked {c['timestamp']}) — {c['status']}")
        blocks.append("\n".join(lines))

    if inbound_ben_messages:
        blocks.append(
            "=== Messages from Ben since last session ===\n"
            + "\n\n".join(f"Ben: {m}" for m in inbound_ben_messages)
        )

    blocks.append(
        "You are awake. This session proceeds in ticks. Each tick you may use "
        "tools (research, files, memory, sub-agents, consolidation) in any "
        "sequence, and you conclude the tick by calling end_tick. You remain present across ticks "
        "and can see everything since this session began. Call end_tick with "
        "end_session=true whenever you want the session to end; if you want to "
        "wake again, set next_invoke_minutes on that tick. To keep an episode "
        f"past the {_fmt_hours(decay_hours)}h decay horizon, consolidate it by id."
    )
    return "\n\n".join(blocks)


def _fmt_hours(value: float) -> str:
    f = float(value)
    return str(int(f)) if f == int(f) else str(f)


# --------------------------------------------------------------------------- #
# model calls + caching
# --------------------------------------------------------------------------- #

def _system_blocks(prompt: str, caching: bool) -> list[dict[str, Any]]:
    block: dict[str, Any] = {"type": "text", "text": prompt}
    if caching:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def _with_rolling_cache(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of messages with a cache breakpoint on the last (user) block,
    so the growing transcript prefix is cached across ticks. Does not mutate the
    stored messages (so breakpoints don't accumulate)."""
    if not messages:
        return messages
    out = list(messages)
    last = out[-1]
    content = last.get("content")
    if isinstance(content, str):
        new_content: Any = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        new_content = [dict(b) for b in content]
        new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
    else:
        return messages
    out[-1] = {**last, "content": new_content}
    return out


def _call_model(
    client: Any, *, model: str, max_tokens: int,
    system_blocks: list[dict[str, Any]], messages: list[dict[str, Any]],
    tools: list[dict[str, Any]], caching: bool,
) -> Any:
    return client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=_with_rolling_cache(messages) if caching else messages,
        tools=tools,
    )


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    try:
        return len(json.dumps(messages, default=str)) // 4
    except Exception:
        return 0


def compact_session_transcript(
    messages: list[dict[str, Any]], client: Any, model: str
) -> None:
    """Summarize everything after the initial context block into one note, in
    place. Safe to call only at a tick boundary (no open tool_use/tool_result)."""
    if len(messages) <= 2:
        return
    head = messages[0]
    try:
        transcript = json.dumps(messages[1:], default=str)[:200_000]
        resp = client.messages.create(
            model=model,
            max_tokens=1500,
            system="Summarize this agent's in-session transcript so far. Preserve "
                   "decisions, facts learned, things written, and current state. "
                   "Be concise and factual.",
            messages=[{"role": "user", "content": transcript}],
        )
        summary = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
    except Exception:
        log.exception("compaction summary failed; leaving transcript intact")
        return
    messages.clear()
    messages.append(head)
    messages.append({
        "role": "user",
        "content": f"[Earlier in this session, summarized to save context]\n{summary}",
    })
    log.info("compacted in-session transcript")


# --------------------------------------------------------------------------- #
# one tick
# --------------------------------------------------------------------------- #

@dataclass
class TickResult:
    tick_state: dict[str, Any]
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    actions: list[str] = field(default_factory=list)
    consolidated_ids: list[int] = field(default_factory=list)


def run_one_tick(
    *, client: Any, model: str, max_tokens: int, caching: bool,
    system_blocks: list[dict[str, Any]], messages: list[dict[str, Any]],
    tools: list[dict[str, Any]], ctx: ToolContext, episodic: EpisodicStore,
    session_id: int,
) -> TickResult:
    ctx.tick_state = None
    counter: Counter = Counter()
    res = TickResult(tick_state={})
    tool_calls = 0

    while True:
        resp = _call_model(
            client, model=model, max_tokens=max_tokens,
            system_blocks=system_blocks, messages=messages, tools=tools, caching=caching,
        )
        tin, tout, cr, cc = _usage(resp)
        res.tokens_in += tin
        res.tokens_out += tout
        res.cache_read += cr
        res.cache_creation += cc
        actual = getattr(getattr(resp, "usage", None), "actual_cost_usd", None)
        res.cost_usd += _turn_cost(model, input_tokens=tin, output_tokens=tout, cache_read=cr, cache_creation=cc, actual_cost=actual)

        messages.append({"role": "assistant", "content": resp.content})
        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]

        if not tool_uses:
            # Model emitted text without concluding. Nudge it to use end_tick.
            messages.append({
                "role": "user",
                "content": "When you are ready, conclude this tick by calling the end_tick tool.",
            })
            tool_calls += 1
            if tool_calls >= MAX_TICK_TOOL_CALLS:
                ctx.tick_state = {
                    "tick_focus": "(tick ended by safety limit; end_tick not called)",
                    "internal_state": None, "journal_entry": None, "slack_to_ben": None,
                    "capability_request": None, "end_session": False, "next_invoke_minutes": None,
                }
                break
            continue

        tool_results = []
        for tu in tool_uses:
            t0 = time.monotonic()
            result = dispatch(tu.name, tu.input or {}, ctx)
            dur_ms = int((time.monotonic() - t0) * 1000)
            counter[tu.name] += 1
            tool_calls += 1
            if tu.name == "consolidate" and isinstance(result, dict):
                res.consolidated_ids.extend(result.get("consolidated") or [])
            err = result.get("error") if isinstance(result, dict) else None
            try:
                episodic.log_action(
                    session_id, tu.name, json.dumps(tu.input or {}, default=str)[:2000],
                    json.dumps(result, default=str)[:1000], err, dur_ms,
                )
            except Exception:
                pass
            payload = json.dumps(result, default=str)
            if len(payload) > TOOL_RESULT_TRUNC:
                payload = payload[:TOOL_RESULT_TRUNC] + "…[truncated]"
            tool_results.append({
                "type": "tool_result", "tool_use_id": tu.id, "content": payload,
            })

        messages.append({"role": "user", "content": tool_results})

        if ctx.tick_state is not None:
            break
        if tool_calls >= MAX_TICK_TOOL_CALLS:
            ctx.tick_state = {
                "tick_focus": "(tick ended by safety limit; end_tick not called)",
                "internal_state": None, "journal_entry": None, "slack_to_ben": None,
                "capability_request": None, "end_session": False, "next_invoke_minutes": None,
            }
            break

    res.tick_state = ctx.tick_state or {
        "tick_focus": "(no tick_state)", "end_session": False, "next_invoke_minutes": None,
    }
    res.tool_calls = tool_calls
    res.actions = [
        f"{name} ({n})" if n > 1 else name for name, n in counter.most_common()
    ]
    return res


# --------------------------------------------------------------------------- #
# main session
# --------------------------------------------------------------------------- #

def run_v2_session(instance: Instance) -> int:
    rt = setup_session(instance)
    if rt is None:
        return 0
    cfg = rt.cfg

    # v2-specific: agent-controlled wake (end_session) + a fixed inter-tick interval.
    min_wake_hours = float(cfg.get("min_wake_hours", 2))
    min_wake_seconds = min_wake_hours * 3600
    tick_interval = int(cfg.get("tick_interval_seconds", 300))
    min_interval_minutes = int(cfg.get("min_interval_minutes", 30))

    system_blocks = _system_blocks(
        build_v2_system_prompt(min_wake_hours=min_wake_hours, decay_hours=rt.decay_hours), rt.caching
    )
    tools = tools_with_cache_control(TOOLS_SPEC_V2, rt.caching)
    user0 = build_session_context(
        instance=instance, episodic=rt.episodic, semantic=rt.semantic,
        decayed=rt.decayed, inbound_ben_messages=[m["text"] for m in rt.inbound_dms],
        decay_hours=rt.decay_hours,
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": user0}]

    session_cost = 0.0
    total_cache_read = 0
    total_tool_calls = 0
    consolidated_count = 0
    num_ticks = 0
    end_reason = "agent_ended"
    next_invoke_minutes: int | None = None
    fatal_error: str | None = None

    # Step 5 — tick loop (the agent ends the session via end_session).
    while True:
        capped = cost_caps_tripped(rt, session_cost)
        if capped:
            end_reason = capped
            break
        elapsed = time.monotonic() - rt.t_start
        if elapsed >= rt.max_wall:
            end_reason = "wall_clock_cap"
            log.warning("wall-clock cap reached (%.0fs); ending session", elapsed)
            break
        if num_ticks >= MAX_TICKS_PER_SESSION:
            end_reason = "max_ticks"
            break

        try:
            tick = run_one_tick(
                client=rt.client, model=rt.model, max_tokens=rt.max_tokens, caching=rt.caching,
                system_blocks=system_blocks, messages=messages, tools=tools,
                ctx=rt.ctx, episodic=rt.episodic, session_id=rt.session_id,
            )
        except Exception as exc:  # noqa: BLE001 — model/API error must not zombie the session
            log.exception("Fatal error during tick %d; ending session", num_ticks + 1)
            fatal_error = f"{type(exc).__name__}: {exc}"
            end_reason = "session_error"
            break
        num_ticks += 1
        session_cost += tick.cost_usd
        total_cache_read += tick.cache_read
        total_tool_calls += tick.tool_calls
        consolidated_count += len(tick.consolidated_ids)
        try:
            rt.episodic.add_cost(tokens_in=tick.tokens_in, tokens_out=tick.tokens_out, cost_usd=tick.cost_usd)
        except Exception:
            pass

        ts = tick.tick_state
        elapsed = time.monotonic() - rt.t_start
        # v2 records the agent's chosen next wake only when it ends the session.
        nim = ts.get("next_invoke_minutes") if ts.get("end_session") else None
        record_episode(rt, ts, tick, elapsed=elapsed, next_invoke_minutes=nim)

        log.info("tick %d: focus=%r end_session=%s tools=%d cost=$%.4f cache_r=%d",
                 num_ticks, ts.get("tick_focus"), ts.get("end_session"),
                 tick.tool_calls, tick.cost_usd, tick.cache_read)

        if ts.get("end_session"):
            end_reason = "agent_ended"
            next_invoke_minutes = ts.get("next_invoke_minutes")
            break

        if rt.compaction_on and _estimate_tokens(messages) > rt.compaction_threshold:
            compact_session_transcript(messages, rt.client, rt.model)

        if tick_interval > 0:
            time.sleep(tick_interval)

    # Step 6 — finalize.
    ended_at = now_iso()
    elapsed = time.monotonic() - rt.t_start
    ended_early = bool(end_reason == "agent_ended" and elapsed < min_wake_seconds)

    if isinstance(next_invoke_minutes, (int, float)):
        nim = int(next_invoke_minutes)
        if nim < min_interval_minutes:
            log.warning("next_invoke_minutes=%d clamped to %d", nim, min_interval_minutes)
            nim = min_interval_minutes
        next_invoke_minutes = nim
    else:
        next_invoke_minutes = None

    finalize_stats(rt, end_reason=end_reason, fatal_error=fatal_error,
                   num_ticks=num_ticks, session_cost=session_cost, total_tool_calls=total_tool_calls)
    try:
        rt.episodic.log_v2_session(
            session_id=rt.session_id, started_at=rt.started_at, ended_at=ended_at,
            num_ticks=num_ticks, elapsed_seconds=elapsed, min_wake_seconds=min_wake_seconds,
            ended_early=ended_early, end_reason=end_reason, next_invoke_minutes=next_invoke_minutes,
            decayed_count=len(rt.decayed), consolidated_count=consolidated_count,
        )
    except Exception:
        log.exception("Failed to write v2 session record")

    if rt.inbound_dms:
        try:
            rt.episodic.set_meta("last_seen_ben_dm_ts", max(m["ts"] for m in rt.inbound_dms))
        except Exception:
            pass

    # Fatal error: pause + notify instead of the normal summary/reschedule.
    if fatal_error:
        fatal_pause(rt, fatal_error)
        log.error("v2 session %d ended on fatal error: %s", rt.session_id, fatal_error)
        return 0

    log.info(
        "v2 session %d ended: reason=%s ticks=%d elapsed=%.1fm min_wake=%.1fm early=%s "
        "cost=$%.4f cache_read=%d consolidated=%d decayed=%d next_invoke=%s",
        rt.session_id, end_reason, num_ticks, elapsed / 60, min_wake_seconds / 60,
        ended_early, session_cost, total_cache_read, consolidated_count, len(rt.decayed),
        next_invoke_minutes,
    )

    if rt.slack:
        summary = (
            f":information_source: *v2 session #{rt.invocation_num} ended* — {end_reason}\n"
            f"ticks: {num_ticks}  ·  awake: {elapsed/60:.1f} min "
            f"(reference {min_wake_seconds/60:.0f} min){' · ended before reference' if ended_early else ''}\n"
            f"consolidated: {consolidated_count}  ·  decayed at start: {len(rt.decayed)}  ·  "
            f"cost: ${session_cost:.4f}  ·  next wake: "
            + (f"{next_invoke_minutes} min" if next_invoke_minutes else "none")
        )
        rt.slack.post_to_observer_channel(summary)

    # Step 7 — budget recheck + research panel + schedule next wake.
    if not post_session_budget_ok(rt):
        return 0
    run_post_session_panel(rt)
    schedule_next_wake(rt, next_invoke_minutes)
    return 0
