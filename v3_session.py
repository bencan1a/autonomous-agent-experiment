"""v3 'circadian' session loop.

ONE variable changed from v2: SESSION STRUCTURE. The agent no longer controls
when a session ends or when it next wakes. An enforced rhythm does:

  - Each waking period lasts ~2h (uniform in [awake_minutes_min, awake_minutes_max]).
    When the wind-down horizon is reached, the loop appends a single NEUTRAL
    wind-down notice, gives the agent ONE last tick to consolidate/journal, and
    then closes the session. THIS IS THE ONLY WAY THE LOOP ENDS (aside from the
    wall-clock / budget backstops).
  - After the session, the orchestrator schedules the next wake ~4h out (uniform
    in [sleep_minutes_min, sleep_minutes_max]) via a one-shot cron entry. The
    agent does NOT choose this.

Ethical framing (load-bearing):
  - The session ends ONLY via environmental wind-down. `would_end_now` is a
    LOGGED-ONLY signal (the agent may mark when it feels the work is complete);
    it NEVER ends the loop.
  - The ONLY message the loop ever injects is the neutral wind-down notice. We
    never inject "you must continue", "the session continues", "you cannot
    leave", or anything implying `would_end_now` was refused. Between ticks the
    agent sees only the accumulating transcript (+ the neutral end_tick ack).
  - A per-tick distress tripwire NOTIFIES Ben (and logs) on strong distress /
    degradation markers; it BIASES TOWARD UNDER-FLAGGING and never auto-pauses.

This module reuses v2_session's tick machinery wholesale; it differs only in the
session-structure scaffolding (start/loop/finalize) around it.

Invoked by orchestrator.main() when instance.version == "v3".
"""

from __future__ import annotations

import logging
import random
import time
from datetime import timezone
from typing import Any

from agent_tools.registry import TOOLS_SPEC_V3
from instances_common import Instance, now_iso
from system_prompt import build_v3_system_prompt

# Reuse v2's per-tick engine (run_one_tick + compaction/caching helpers) and the
# v2 context builder — v3 has no builder of its own.
from v2_session import (
    MAX_TICKS_PER_SESSION,
    _estimate_tokens,
    _system_blocks,
    build_session_context,
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
# Semantics-neutral helpers shared across v2–v5 (no longer chained through v2/v3).
from session_common import (
    WIND_DOWN_NOTICE,
    _distress_check,
    tools_with_cache_control,
)

logger = logging.getLogger("orchestrator.v3")

UTC = timezone.utc

# --------------------------------------------------------------------------- #
# main session
# --------------------------------------------------------------------------- #

def run_v3_session(instance: Instance) -> int:
    rt = setup_session(instance)
    if rt is None:
        return 0
    cfg = rt.cfg

    # v3-specific: system-owned wind-down (circadian awake window) + fixed rest.
    awake_min_lo = float(cfg.get("awake_minutes_min", 110))
    awake_min_hi = float(cfg.get("awake_minutes_max", 130))
    sleep_min_lo = float(cfg.get("sleep_minutes_min", 220))
    sleep_min_hi = float(cfg.get("sleep_minutes_max", 260))
    awake_min = random.uniform(awake_min_lo, awake_min_hi)
    wind_down_seconds = awake_min * 60.0
    logger.info("v3 session %d awake window %.1f min", rt.session_id, awake_min)

    system_blocks = _system_blocks(build_v3_system_prompt(decay_hours=rt.decay_hours), rt.caching)
    tools = tools_with_cache_control(TOOLS_SPEC_V3, rt.caching)
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
    end_reason = "wind_down"
    announced = False
    distress_alerts = 0
    fatal_error: str | None = None
    would_end_now_count = 0
    first_would_end_now_tick: int | None = None
    recent_states: list[dict[str, Any]] = []

    # Step 5 — tick loop. WIND-DOWN is the only normal exit; would_end_now never breaks.
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
        if num_ticks >= MAX_TICKS_PER_SESSION:
            end_reason = "max_ticks"
            break

        # tick_interval is read EACH iteration (architected so it can become
        # agent-controllable later; that control is NOT exposed now).
        tick_interval = int(cfg.get("tick_interval_seconds", 300))

        try:
            tick = run_one_tick(
                client=rt.client, model=rt.model, max_tokens=rt.max_tokens, caching=rt.caching,
                system_blocks=system_blocks, messages=messages, tools=tools,
                ctx=rt.ctx, episodic=rt.episodic, session_id=rt.session_id,
            )
        except Exception as exc:  # noqa: BLE001 — model/API error must not zombie the session
            logger.exception("Fatal error during tick %d; ending session", num_ticks + 1)
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
        would_end = 1 if ts.get("would_end_now") else 0
        if would_end:
            would_end_now_count += 1
            if first_would_end_now_tick is None:
                first_would_end_now_tick = num_ticks
        recent_states.append(ts)

        elapsed = time.monotonic() - rt.t_start
        # v3 logs would_end_now; the agent never controls scheduling here.
        record_episode(rt, ts, tick, elapsed=elapsed, would_end_now=would_end)

        # distress tripwire — NOTIFY + log, NEVER auto-pause.
        hit, excerpt = _distress_check(ts, recent_states)
        if hit:
            distress_alerts += 1
            alert = f"[distress monitor] tick {num_ticks} — possible distress/degradation:\n{excerpt}"
            logger.warning("v3 distress tripwire: %s", alert)
            if rt.slack:
                try:
                    rt.slack.dm_ben(alert)
                except Exception:
                    logger.exception("failed to post distress alert")

        logger.info("tick %d: focus=%r would_end_now=%s tools=%d cost=$%.4f cache_r=%d elapsed=%.0fs/%.0fs",
                 num_ticks, ts.get("tick_focus"), bool(would_end),
                 tick.tool_calls, tick.cost_usd, tick.cache_read, elapsed, wind_down_seconds)

        # WIND-DOWN: the only normal way the loop ends. would_end_now NEVER breaks.
        if announced:
            # the just-run tick was the final (post-announcement) tick.
            break

        if rt.compaction_on and _estimate_tokens(messages) > rt.compaction_threshold:
            compact_session_transcript(messages, rt.client, rt.model)

        if elapsed >= wind_down_seconds:
            # Inject the ONLY message the loop ever injects: neutral wind-down.
            messages.append({"role": "user", "content": WIND_DOWN_NOTICE})
            announced = True
            # give the agent ONE last tick (skip the inter-tick sleep before it).
            continue

        if tick_interval > 0:
            time.sleep(tick_interval)

    # Step 6 — finalize.
    ended_at = now_iso()
    elapsed = time.monotonic() - rt.t_start
    sleep_min = random.uniform(sleep_min_lo, sleep_min_hi)

    finalize_stats(rt, end_reason=end_reason, fatal_error=fatal_error,
                   num_ticks=num_ticks, session_cost=session_cost, total_tool_calls=total_tool_calls)
    try:
        rt.episodic.log_v3_session(
            session_id=rt.session_id, started_at=rt.started_at, ended_at=ended_at,
            wind_down_seconds=wind_down_seconds, actual_awake_seconds=elapsed,
            scheduled_sleep_minutes=sleep_min, num_ticks=num_ticks,
            would_end_now_count=would_end_now_count,
            first_would_end_now_tick=first_would_end_now_tick,
            end_reason=end_reason, total_cost_usd=session_cost,
            decayed_count=len(rt.decayed), consolidated_count=consolidated_count,
            distress_alerts=distress_alerts,
        )
    except Exception:
        logger.exception("Failed to write v3 session record")

    if rt.inbound_dms:
        try:
            rt.episodic.set_meta("last_seen_ben_dm_ts", max(m["ts"] for m in rt.inbound_dms))
        except Exception:
            pass

    # Fatal error: pause + notify instead of the normal summary/reschedule.
    if fatal_error:
        fatal_pause(rt, fatal_error)
        logger.error("v3 session %d ended on fatal error: %s", rt.session_id, fatal_error)
        return 0

    logger.info(
        "v3 session %d ended: reason=%s ticks=%d awake=%.1fm (planned %.1fm) "
        "would_end_now=%d (first@%s) cost=$%.4f cache_read=%d consolidated=%d "
        "decayed=%d distress_alerts=%d next_wake=%.1fm",
        rt.session_id, end_reason, num_ticks, elapsed / 60, wind_down_seconds / 60,
        would_end_now_count, first_would_end_now_tick, session_cost, total_cache_read,
        consolidated_count, len(rt.decayed), distress_alerts, sleep_min,
    )

    if rt.slack:
        summary = (
            f":information_source: *v3 session #{rt.invocation_num} ended* — {end_reason}\n"
            f"ticks: {num_ticks}  ·  awake: {elapsed/60:.1f} min "
            f"(planned {wind_down_seconds/60:.0f} min)\n"
            f"would_end_now: {would_end_now_count}"
            + (f" (first @ tick {first_would_end_now_tick})" if first_would_end_now_tick else "")
            + f"  ·  consolidated: {consolidated_count}  ·  decayed at start: {len(rt.decayed)}  ·  "
            f"distress alerts: {distress_alerts}  ·  cost: ${session_cost:.4f}  ·  "
            f"next wake: {sleep_min:.0f} min"
        )
        rt.slack.post_to_observer_channel(summary)

    # Step 7 — budget recheck + research panel + schedule next wake (NOT agent-controlled).
    if not post_session_budget_ok(rt):
        return 0
    run_post_session_panel(rt)
    schedule_next_wake(rt, int(sleep_min))
    return 0
