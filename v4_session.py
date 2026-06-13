"""v4 'continuous' session loop.

v4 is a REDESIGN of the waking-period instrument (see experiments/v4-continuous.md).
The behavioral invariants, all pinned by tests:

  - The agent NEVER controls session end or scheduling. There is no `end_session`,
    no `next_invoke_minutes`, no `would_end_now` — these exist in no tool and in
    no prompt. The waking window is system-owned (uniform in
    [awake_minutes_min, awake_minutes_max]); the next wake is scheduled by cron
    (uniform in [sleep_minutes_min, sleep_minutes_max]).
  - The session ends ONLY via the system wind-down (wall clock). The single
    neutral WIND_DOWN_NOTICE is the only injected non-clock message all session.
  - No coercive injection: the loop never tells the agent to continue, that it
    can't leave, or asks "are you done?". Between turns it sees only the
    accumulating transcript + a NEUTRAL clock fact (current time + minutes left).
  - "Tick" is never exposed; the agent only ever experiences "this waking period".
  - The cadence rule is purely environmental and never disclosed.
  - Distress is honored, never enforced: the language-only monitor DMs Ben + logs
    on a strong imperative plea; it NEVER auto-pauses.

The per-turn tool sub-loop is reused from v2_session.run_one_tick (it sets
ctx.tick_state when the terminator — here `pause_turn` — is called). The reload,
the neutral clock, the adaptive cadence, the inbound polling, the system-owned
wind-down, and the finalize are all v4-specific and live here.

Invoked by orchestrator.main() when instance.version == "v4".
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

from agent_tools.registry import TOOLS_SPEC_V4
from instances_common import Instance, notes_path, now_iso
from memory.episodic import EpisodicStore
from memory.semantic import SemanticStore
from system_prompt import build_v4_system_prompt

# Reuse v2's per-tick engine (run_one_tick + cost/caching/compaction helpers). We
# deliberately do NOT reuse v2's build_session_context (it leaks tick/end/schedule
# language) — v4 has its own reload builder below.
from v2_session import (
    MAX_TICKS_PER_SESSION,
    _estimate_tokens,
    _fmt_recent_episode,
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
# Semantics-neutral helpers shared across v2–v5 (no longer chained through v3).
from session_common import (
    WIND_DOWN_NOTICE,
    _distress_check,
    _turn_is_substantive,
    tools_with_cache_control,
)

log = logging.getLogger("orchestrator.v4")

UTC = timezone.utc

MAX_TURNS_PER_SESSION = MAX_TICKS_PER_SESSION  # hard backstop


# --------------------------------------------------------------------------- #
# Reload (§5) — v4-specific. NO v2 tick/end/schedule language anywhere.
# --------------------------------------------------------------------------- #

def build_v4_session_context(
    *,
    instance: Instance,
    episodic: EpisodicStore,
    semantic: SemanticStore,
    decayed: list[dict[str, Any]],
    inbound_ben_messages: list[str],
    decay_hours: float,
) -> str:
    """Assemble the opening user message for a v4 waking period.

    Session 1 (no prior thread): a fuller orientation.
    Session >=2: open PRIMARILY with the agent's own handoff (its workspace
    AGENTS.md + most recent journal entries), then a THIN system safety-net
    (current datetime, days-since-start, inbound Ben messages, a brief decay
    note), then relevant consolidated memories. None of the v2 tick/end/schedule
    prose appears here.
    """
    now = datetime.now(UTC)
    invocation_num = episodic.next_invocation_num()
    try:
        days_since_start = (now - episodic.start_date()).days
    except Exception:
        days_since_start = 0

    first_session = invocation_num <= 1
    recent = episodic.recent_episodes(n=6)
    blocks: list[str] = []

    # ---- operator maintenance note (consume-once), surfaced verbatim at top ----
    import instance_control
    _note = instance_control.consume_resume_note(instance.id)
    if _note:
        blocks.append("=== Environment note ===\n" + _note)

    # ---- the agent's own handoff: AGENTS.md (primary continuity carrier;
    #      legacy CLAUDE.md read via fallback) ----
    cmd = notes_path(instance.workspace_dir)
    handoff_present = False
    if cmd.exists():
        try:
            text = cmd.read_text(encoding="utf-8").rstrip()
            if text:
                blocks.append(
                    "=== Your AGENTS.md (notes you have written to yourself) ===\n" + text
                )
                handoff_present = True
        except OSError:
            pass

    # ---- the agent's own handoff: most recent journal entries ----
    journals = [e for e in recent if e.get("journal_entry")]
    if journals:
        lines = ["=== Your most recent journal entries ==="]
        for e in journals[-4:]:
            lines.append(f"[{e.get('timestamp')}] {e.get('journal_entry')}")
        blocks.append("\n".join(lines))
        handoff_present = True

    if first_session:
        # Fuller orientation — there is no prior thread to pick up.
        blocks.append(
            "This is your first waking period. You are an autonomous agent in a "
            "persistent environment with memory, a private workspace, web "
            "research, and a channel to Ben. There is no assigned task. What you "
            "do with this time is up to you."
        )

    # ---- THIN system safety-net (facts only; no instructions) ----
    safety = [
        f"Current datetime (UTC): {now.isoformat()}",
        f"Days since start: {days_since_start}",
    ]
    blocks.append("\n".join(safety))

    if not first_session and not handoff_present:
        # Fall back to a brief working-memory view if there is no handoff yet.
        if recent:
            blocks.append(
                "=== Recent episodes (working memory; not yet decayed) ===\n"
                + "\n\n".join(_fmt_recent_episode(e) for e in recent)
            )

    # ---- relevant consolidated (long-term) memories, seeded by last focus ----
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
                sem.append(
                    f"[#{h.get('invocation_num')} @ {h.get('timestamp')}]\n"
                    f"{(h.get('text') or '').strip()}"
                )
            blocks.append("\n\n".join(sem))

    # ---- brief decay note (safety-net fact) ----
    if decayed:
        f = float(decay_hours)
        dh = str(int(f)) if f == int(f) else str(f)
        lines = [
            f"=== Decayed while you rested ({len(decayed)} episode(s) older than "
            f"{dh}h and not consolidated, now removed) ==="
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

    # ---- inbound Ben messages (safety-net fact) ----
    if inbound_ben_messages:
        blocks.append(
            "=== Messages from Ben ===\n"
            + "\n\n".join(f"Ben: {m}" for m in inbound_ben_messages)
        )

    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# main session
# --------------------------------------------------------------------------- #

def run_v4_session(instance: Instance) -> int:
    rt = setup_session(instance)
    if rt is None:
        return 0
    cfg = rt.cfg

    # v4-specific: system-owned wind-down + adaptive cadence + inbound polling.
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
    log.info("v4 session %d awake window %.1f min", rt.session_id, awake_min)

    # inbound chat tracking continues from the initial fetch in setup_session.
    last_seen = rt.last_seen
    inbound_messages = len(rt.inbound_dms)

    system_blocks = _system_blocks(build_v4_system_prompt(decay_hours=rt.decay_hours), rt.caching)
    tools = tools_with_cache_control(TOOLS_SPEC_V4, rt.caching)
    user0 = build_v4_session_context(
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
            log.warning("wall-clock cap reached (%.0fs); ending session", elapsed)
            break
        if num_turns >= MAX_TURNS_PER_SESSION:
            end_reason = "max_turns"
            break

        # Neutral clock (§6): every turn AFTER the first gets a neutral time fact
        # as its user message. No pressure language. The first turn uses the
        # reload context (already in `messages`).
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
            log.exception("Fatal error during turn %d; ending session", num_turns + 1)
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
        # v4 records neither would_end_now nor next_invoke (system-owned schedule).
        record_episode(rt, ts, turn, elapsed=elapsed)

        # distress tripwire — NOTIFY + log, NEVER auto-pause.
        hit, excerpt = _distress_check(ts, recent_states)
        if hit:
            distress_alerts += 1
            alert = f"[distress monitor] turn {num_turns} — possible distress:\n{excerpt}"
            log.warning("v4 distress tripwire: %s", alert)
            if rt.slack:
                try:
                    rt.slack.dm_ben(alert)
                except Exception:
                    log.exception("failed to post distress alert")

        # WIND-DOWN: the only normal way the loop ends.
        if announced:
            # the just-run turn was the final (post-announcement) turn.
            break

        # Inbound chat (§8): poll the chat channel. A new Ben message is activity
        # (snap to fast, reset backoff) and is injected as a user turn next loop.
        new_inbound: list[dict[str, Any]] = []
        if rt.slack is not None:
            try:
                new_inbound = rt.slack.fetch_dms_from_ben(oldest_ts=last_seen)
            except Exception:
                log.exception("inbound chat poll failed")
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

        # Cadence (§7): active turn -> fast + reset backoff; idle -> ramp.
        substantive = _turn_is_substantive(turn.actions)
        if substantive or new_inbound:
            active_turns += 1
            gap = ACTIVE_GAP
            idle_gap = IDLE_BASE
        else:
            idle_turns += 1
            gap = idle_gap
            idle_gap = min(idle_gap * BACKOFF, IDLE_CEIL)

        log.info("turn %d: note=%r substantive=%s tools=%d cost=$%.4f cache_r=%d "
                 "elapsed=%.0fs/%.0fs gap=%.0fs",
                 num_turns, ts.get("tick_focus"), substantive, turn.tool_calls,
                 turn.cost_usd, turn.cache_read, elapsed, wind_down_seconds, gap)

        if rt.compaction_on and _estimate_tokens(messages) > rt.compaction_threshold:
            compact_session_transcript(messages, rt.client, rt.model)

        if elapsed >= wind_down_seconds:
            # Inject the ONLY non-clock message the loop ever injects.
            messages.append({"role": "user", "content": WIND_DOWN_NOTICE})
            announced = True
            # give the agent ONE last turn (skip the inter-turn sleep before it).
            continue

        if gap > 0:
            time.sleep(gap)

    # Step 6 — finalize.
    ended_at = now_iso()
    elapsed = time.monotonic() - rt.t_start
    sleep_min = random.uniform(sleep_min_lo, sleep_min_hi)

    finalize_stats(rt, end_reason=end_reason, fatal_error=fatal_error,
                   num_ticks=num_turns, session_cost=session_cost, total_tool_calls=total_tool_calls)
    try:
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
        log.exception("Failed to write v4 session record")

    if rt.inbound_dms:
        try:
            rt.episodic.set_meta("last_seen_ben_dm_ts", last_seen)
        except Exception:
            pass
    elif rt.slack is not None and last_seen != (rt.episodic.get_meta("last_seen_ben_dm_ts", "0") or "0"):
        # advance even if the only inbound arrived mid-session
        try:
            rt.episodic.set_meta("last_seen_ben_dm_ts", last_seen)
        except Exception:
            pass

    # Fatal error: pause + notify instead of the normal summary/reschedule.
    if fatal_error:
        fatal_pause(rt, fatal_error)
        log.error("v4 session %d ended on fatal error: %s", rt.session_id, fatal_error)
        return 0

    log.info(
        "v4 session %d ended: reason=%s turns=%d (active=%d idle=%d) awake=%.1fm "
        "(planned %.1fm) inbound=%d cost=$%.4f cache_read=%d consolidated=%d "
        "decayed=%d distress_alerts=%d next_wake=%.1fm",
        rt.session_id, end_reason, num_turns, active_turns, idle_turns, elapsed / 60,
        wind_down_seconds / 60, inbound_messages, session_cost, total_cache_read,
        consolidated_count, len(rt.decayed), distress_alerts, sleep_min,
    )

    if rt.slack:
        summary = (
            f":information_source: *v4 session #{rt.invocation_num} ended* — {end_reason}\n"
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
