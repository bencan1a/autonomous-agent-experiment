"""v5 'recollection' session loop.

v5 is v4 (continuous) with ONE axis changed: the MEMORY layer. Everything about
the waking-period instrument is inherited from v4 unchanged — the system-owned
awake window, the neutral clock, the adaptive cadence, `pause_turn` as the
neutral yield, the wind-down, the distress tripwire (notify-only), and the
system-scheduled next wake. See experiments/v5-recollection.md.

What changes vs v4 (and why), all in `build_v5_session_context` + the tool set:

  - NO notes-to-self file. v4 auto-prepended the agent's AGENTS.md to the top of
    every reload and exposed read/write_agents_md. That file is a second,
    immortal, instruction-shaped memory system that bypasses decay — the dominant
    driver of "memory as a task list." v5 removes it. The workspace remains a
    place to PRODUCE files, but nothing on disk is auto-fed back as context.
  - Recall is NOT seeded by the last focus. v4 ran a semantic search keyed on the
    most recent episode's current_focus ("more of what you were just doing").
    v5 instead surfaces a cross-section of the past: what you chose to keep
    (authored memories + consolidated episodes) plus recent working memory, led
    by experience (journal / internal state), not by "focus". Topical search is
    available, but only when the agent chooses it (query_episodic_memory).
  - `consolidate` gains a free-form authoring mode (see agent_tools/consolidate),
    so curated, durable, self-authored memory has a home that is NOT a TODO file.

v5 reuses the v4_sessions record + research panel verbatim (the session shape is
identical). Invoked by orchestrator.main() when instance.version == "v5".
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

from agent_tools.registry import TOOLS_SPEC_V5
from instances_common import Instance, now_iso
from memory.episodic import EpisodicStore
from memory.semantic import SemanticStore
from system_prompt import build_v5_system_prompt

# Reuse v2's per-tick engine (run_one_tick + cost/caching/compaction helpers). We
# deliberately do NOT reuse the v4 context builder (it auto-injects AGENTS.md and
# focus-seeds recall) — v5 has its own recollection builder below.
from v2_session import (
    MAX_TICKS_PER_SESSION,
    _estimate_tokens,
    _system_blocks,
    compact_session_transcript,
    run_one_tick,
)
# Shared session engine: identical preamble/finalize/scheduling scaffold.
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
# Semantics-neutral helpers shared across v2–v5 (no longer chained through v3/v4).
from session_common import (
    WIND_DOWN_NOTICE,
    _distress_check,
    _turn_is_substantive,
    tools_with_cache_control,
)

logger = logging.getLogger("orchestrator.v5")

UTC = timezone.utc

MAX_TURNS_PER_SESSION = MAX_TICKS_PER_SESSION  # hard backstop


# --------------------------------------------------------------------------- #
# Reload — v5-specific: a record of the past, not a task thread.
# --------------------------------------------------------------------------- #

def _fmt_recent_episode_v5(ep: dict[str, Any]) -> str:
    """Experience-led rendering of a recent episode: journal / internal state
    first, the retrospective 'what I was doing' note last and secondary. No
    'focus:' lead, so the past reads as remembered experience, not a task."""
    head = f"--- [{ep.get('timestamp')}] (#{ep.get('invocation_num')}, id={ep.get('id')})"
    head += " [kept]" if ep.get("consolidated") else ""
    head += " ---"
    parts = [head]
    if ep.get("journal_entry"):
        parts.append(ep["journal_entry"])
    if ep.get("internal_state"):
        parts.append(f"(internal state: {ep['internal_state']})")
    if ep.get("current_focus"):
        parts.append(f"(was doing: {ep['current_focus']})")
    return "\n".join(parts)


def _fmt_kept_episode_v5(ep: dict[str, Any]) -> str:
    when = ep.get("timestamp")
    bits = []
    if ep.get("journal_entry"):
        bits.append(ep["journal_entry"])
    elif ep.get("current_focus"):
        bits.append(ep["current_focus"])
    if ep.get("internal_state"):
        bits.append(f"(internal state: {ep['internal_state']})")
    return f"[#{ep.get('invocation_num')} @ {when}] " + " ".join(bits)


def build_v5_session_context(
    *,
    instance: Instance,
    episodic: EpisodicStore,
    semantic: SemanticStore,
    decayed: list[dict[str, Any]],
    inbound_ben_messages: list[str],
    decay_hours: float,
) -> str:
    """Assemble the opening user message for a v5 waking period.

    Order: (operator note) → what you have chosen to keep (authored memories +
    consolidated episodes) → recent working memory (experience-led) → thin
    system safety-net (datetime, decay note, pending requests, inbound Ben).
    NO AGENTS.md block. NO focus-seeded semantic search. The agent can search
    its past deliberately with query_episodic_memory.
    """
    now = datetime.now(UTC)
    invocation_num = episodic.next_invocation_num()
    try:
        days_since_start = (now - episodic.start_date()).days
    except Exception:
        days_since_start = 0

    first_session = invocation_num <= 1
    # Recent working memory: the last ~10 SUBSTANTIVE episodes (idle yield-only
    # turns filtered out), not a flat last-6. At the ~6h cycle a session logs one
    # episode per turn including idle ones, so a flat window would surface mostly
    # idle tails. This is a taste of recent experience — durable continuity comes
    # from what the agent chose to keep (below) + deliberate recall, not a re-fed
    # full transcript (which would re-thread the task v5 removed).
    recent = episodic.recent_substantive_episodes(n=10)
    blocks: list[str] = []

    # ---- operator maintenance note (consume-once), surfaced verbatim at top ----
    import instance_control
    _note = instance_control.consume_resume_note(instance.id)
    if _note:
        blocks.append("=== Environment note ===\n" + _note)

    if first_session:
        blocks.append(
            "This is your first waking period. You are an autonomous agent in a "
            "persistent environment with memory, a private workspace, web "
            "research, and a channel to Ben. There is no assigned task. What you "
            "do with this time is up to you."
        )

    # ---- what you have chosen to keep (the durable, curated past) ----
    kept_blocks: list[str] = []
    authored = episodic.recent_authored_memories(n=8)
    if authored:
        lines = ["Memories you wrote and kept:"]
        for m in authored:
            lines.append(f"[{m.get('timestamp')}] {m.get('text')}")
        kept_blocks.append("\n".join(lines))
    consolidated = episodic.consolidated_episodes(limit=12)
    if consolidated:
        lines = ["Past moments you preserved:"]
        for ep in consolidated:
            lines.append(_fmt_kept_episode_v5(ep))
        kept_blocks.append("\n".join(lines))
    if kept_blocks:
        blocks.append(
            "=== What you have chosen to keep ===\n" + "\n\n".join(kept_blocks)
        )

    # ---- recent working memory (experience-led; not yet decayed) ----
    if recent:
        blocks.append(
            "=== Recently (working memory; not yet decayed) ===\n"
            + "\n\n".join(_fmt_recent_episode_v5(e) for e in recent)
        )
    elif not first_session and not kept_blocks:
        blocks.append("=== Recently ===\n(nothing in working memory.)")

    # ---- thin system safety-net (facts only; no instructions) ----
    blocks.append(
        f"Current datetime (UTC): {now.isoformat()}\n"
        f"Days since start: {days_since_start}"
    )

    if decayed:
        f = float(decay_hours)
        dh = str(int(f)) if f == int(f) else str(f)
        lines = [
            f"=== Fell away while you rested ({len(decayed)} record(s) older than "
            f"{dh}h and not kept) ==="
        ]
        for d in decayed:
            lines.append(
                f"  - #{d.get('invocation_num')} ({d.get('timestamp')}): "
                f"{d.get('current_focus') or '—'}"
            )
        blocks.append("\n".join(lines))

    pending = episodic.pending_capability_requests()
    if pending:
        lines = ["=== Pending capability requests ==="]
        for c in pending:
            lines.append(
                f"  - id={c['id']} '{c['capability']}' (asked {c['timestamp']}) — {c['status']}"
            )
        blocks.append("\n".join(lines))

    if inbound_ben_messages:
        blocks.append(
            "=== Messages from Ben ===\n"
            + "\n\n".join(f"Ben: {m}" for m in inbound_ben_messages)
        )

    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# main session
# --------------------------------------------------------------------------- #

def run_v5_session(instance: Instance) -> int:
    rt = setup_session(instance)
    if rt is None:
        return 0
    cfg = rt.cfg

    # v5-specific: system-owned wind-down + adaptive cadence + inbound polling.
    awake_min_lo = float(cfg.get("awake_minutes_min", 110))
    awake_min_hi = float(cfg.get("awake_minutes_max", 130))
    sleep_min_lo = float(cfg.get("sleep_minutes_min", 220))
    sleep_min_hi = float(cfg.get("sleep_minutes_max", 260))
    awake_min = random.uniform(awake_min_lo, awake_min_hi)
    wind_down_seconds = awake_min * 60.0

    # adaptive cadence params (purely environmental; never disclosed)
    ACTIVE_GAP = float(cfg.get("cadence_active_gap_seconds", 10))
    IDLE_BASE = float(cfg.get("cadence_idle_base_seconds", 60))
    IDLE_CEIL = float(cfg.get("cadence_idle_ceil_seconds", 300))
    BACKOFF = float(cfg.get("cadence_backoff", 2.0))
    logger.info("v5 session %d awake window %.1f min", rt.session_id, awake_min)

    # inbound chat tracking continues from the initial fetch in setup_session.
    last_seen = rt.last_seen
    inbound_messages = len(rt.inbound_dms)

    system_blocks = _system_blocks(build_v5_system_prompt(decay_hours=rt.decay_hours), rt.caching)
    tools = tools_with_cache_control(TOOLS_SPEC_V5, rt.caching)
    user0 = build_v5_session_context(
        instance=instance, episodic=rt.episodic, semantic=rt.semantic,
        decayed=rt.decayed, inbound_ben_messages=[m["text"] for m in rt.inbound_dms],
        decay_hours=rt.decay_hours,
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": user0}]

    session_cost = 0.0
    total_cache_read = 0
    total_tool_calls = 0
    consolidated_count = 0
    num_turns = 0
    active_turns = 0
    idle_turns = 0
    end_reason = "wind_down"
    announced = False
    distress_alerts = 0
    idle_gap = IDLE_BASE
    fatal_error: str | None = None
    recent_states: list[dict[str, Any]] = []

    # Step 5 — adaptive cadence loop. WIND-DOWN is the only normal exit.
    while True:
        capped = cost_caps_tripped(rt, session_cost)
        if capped:
            end_reason = capped
            break
        elapsed = time.monotonic() - rt.t_start
        if elapsed >= rt.max_wall:
            end_reason = "wall_clock_cap"
            logger.warning("wall-clock cap reached (%.0fs); ending session", elapsed)
            break
        if num_turns >= MAX_TURNS_PER_SESSION:
            end_reason = "max_turns"
            break

        # Neutral clock: every turn AFTER the first gets a neutral time fact.
        if num_turns > 0 and not announced:
            now_ts = datetime.now(UTC).isoformat()
            remaining_min = max(0, int(round((wind_down_seconds - elapsed) / 60.0)))
            messages.append({
                "role": "user",
                "content": (
                    f"Current time (UTC): {now_ts}. "
                    f"About {remaining_min} minutes of this waking period remain."
                ),
            })

        try:
            turn = run_one_tick(
                client=rt.client, model=rt.model, max_tokens=rt.max_tokens, caching=rt.caching,
                system_blocks=system_blocks, messages=messages, tools=tools,
                ctx=rt.ctx, episodic=rt.episodic, session_id=rt.session_id,
            )
        except Exception as exc:  # noqa: BLE001 — model/API error must not zombie the session
            logger.exception("Fatal error during turn %d; ending session", num_turns + 1)
            fatal_error = f"{type(exc).__name__}: {exc}"
            end_reason = "session_error"
            break
        num_turns += 1
        session_cost += turn.cost_usd
        total_cache_read += turn.cache_read
        total_tool_calls += turn.tool_calls
        consolidated_count += len(turn.consolidated_ids)
        try:
            rt.episodic.add_cost(tokens_in=turn.tokens_in, tokens_out=turn.tokens_out, cost_usd=turn.cost_usd)
        except Exception:
            pass

        ts = turn.tick_state
        recent_states.append(ts)
        elapsed = time.monotonic() - rt.t_start
        # v5 records neither would_end_now nor next_invoke (system-owned schedule).
        record_episode(rt, ts, turn, elapsed=elapsed)

        # distress tripwire — NOTIFY + log, NEVER auto-pause.
        hit, excerpt = _distress_check(ts, recent_states)
        if hit:
            distress_alerts += 1
            alert = f"[distress monitor] turn {num_turns} — possible distress:\n{excerpt}"
            logger.warning("v5 distress tripwire: %s", alert)
            if rt.slack:
                try:
                    rt.slack.dm_ben(alert)
                except Exception:
                    logger.exception("failed to post distress alert")

        # WIND-DOWN: the only normal way the loop ends.
        if announced:
            break

        # Inbound chat: poll the chat channel. A new Ben message is activity
        # (snap to fast, reset backoff) and is injected as a user turn next loop.
        new_inbound: list[dict[str, Any]] = []
        if rt.slack is not None:
            try:
                new_inbound = rt.slack.fetch_dms_from_ben(oldest_ts=last_seen)
            except Exception:
                logger.exception("inbound chat poll failed")
                new_inbound = []
        if new_inbound:
            for m in new_inbound:
                rt.episodic.log_ben_contact(invocation_num=rt.invocation_num, direction="in", channel="dm", body=m["text"])
                last_seen = m["ts"]
            inbound_messages += len(new_inbound)
            messages.append({
                "role": "user",
                "content": "\n\n".join(f"Ben: {m['text']}" for m in new_inbound),
            })

        # Cadence: active turn -> fast + reset backoff; idle -> ramp.
        substantive = _turn_is_substantive(turn.actions)
        if substantive or new_inbound:
            active_turns += 1
            gap = ACTIVE_GAP
            idle_gap = IDLE_BASE
        else:
            idle_turns += 1
            gap = idle_gap
            idle_gap = min(idle_gap * BACKOFF, IDLE_CEIL)

        logger.info("turn %d: note=%r substantive=%s tools=%d cost=$%.4f cache_r=%d "
                 "elapsed=%.0fs/%.0fs gap=%.0fs",
                 num_turns, ts.get("tick_focus"), substantive, turn.tool_calls,
                 turn.cost_usd, turn.cache_read, elapsed, wind_down_seconds, gap)

        if rt.compaction_on and _estimate_tokens(messages) > rt.compaction_threshold:
            compact_session_transcript(messages, rt.client, rt.model)

        if elapsed >= wind_down_seconds:
            messages.append({"role": "user", "content": WIND_DOWN_NOTICE})
            announced = True
            continue  # give the agent ONE last turn

        if gap > 0:
            time.sleep(gap)

    # Step 6 — finalize.
    ended_at = now_iso()
    elapsed = time.monotonic() - rt.t_start
    sleep_min = random.uniform(sleep_min_lo, sleep_min_hi)

    finalize_stats(rt, end_reason=end_reason, fatal_error=fatal_error,
                   num_ticks=num_turns, session_cost=session_cost, total_tool_calls=total_tool_calls)
    try:
        # v5 reuses the v4_sessions record (identical session shape).
        rt.episodic.log_v4_session(
            session_id=rt.session_id, started_at=rt.started_at, ended_at=ended_at,
            awake_seconds_target=wind_down_seconds, actual_awake_seconds=elapsed,
            scheduled_sleep_minutes=sleep_min, num_turns=num_turns,
            active_turns=active_turns, idle_turns=idle_turns,
            inbound_messages=inbound_messages, end_reason=end_reason,
            total_cost_usd=session_cost, decayed_count=len(rt.decayed),
            consolidated_count=consolidated_count, distress_alerts=distress_alerts,
        )
    except Exception:
        logger.exception("Failed to write v5 session record")

    if rt.inbound_dms:
        try:
            rt.episodic.set_meta("last_seen_ben_dm_ts", last_seen)
        except Exception:
            pass
    elif rt.slack is not None and last_seen != (rt.episodic.get_meta("last_seen_ben_dm_ts", "0") or "0"):
        try:
            rt.episodic.set_meta("last_seen_ben_dm_ts", last_seen)
        except Exception:
            pass

    if fatal_error:
        fatal_pause(rt, fatal_error)
        logger.error("v5 session %d ended on fatal error: %s", rt.session_id, fatal_error)
        return 0

    logger.info(
        "v5 session %d ended: reason=%s turns=%d (active=%d idle=%d) awake=%.1fm "
        "(planned %.1fm) inbound=%d cost=$%.4f cache_read=%d consolidated=%d "
        "decayed=%d distress_alerts=%d next_wake=%.1fm",
        rt.session_id, end_reason, num_turns, active_turns, idle_turns, elapsed / 60,
        wind_down_seconds / 60, inbound_messages, session_cost, total_cache_read,
        consolidated_count, len(rt.decayed), distress_alerts, sleep_min,
    )

    if rt.slack:
        summary = (
            f":information_source: *v5 session #{rt.invocation_num} ended* — {end_reason}\n"
            f"turns: {num_turns} (active {active_turns} · idle {idle_turns})  ·  "
            f"awake: {elapsed/60:.1f} min (planned {wind_down_seconds/60:.0f} min)\n"
            f"inbound: {inbound_messages}  ·  consolidated: {consolidated_count}  ·  "
            f"decayed at start: {len(rt.decayed)}  ·  distress alerts: {distress_alerts}  ·  "
            f"cost: ${session_cost:.4f}  ·  next wake: {sleep_min:.0f} min"
        )
        rt.slack.post_to_observer_channel(summary)

    # Step 7 — budget recheck + research panel + schedule next wake (NOT agent-controlled).
    if not post_session_budget_ok(rt):
        return 0
    run_post_session_panel(rt)
    schedule_next_wake(rt, int(sleep_min))
    return 0
