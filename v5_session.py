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

import atexit
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Any

import anthropic

import cron_control
from openrouter_client import OpenRouterClient, is_openrouter_model
import lockfile
from agent_tools.registry import TOOLS_SPEC_V5, ToolContext
from communications.slack_client import SlackClient, format_episode_for_observer
from instances_common import (
    Instance,
    SHARED_HF_CACHE,
    now_iso,
    registry_txn,
)
from memory.episodic import EpisodicStore
from memory.semantic import SemanticStore
from system_prompt import build_v5_system_prompt
from tools.web_search import BraveSearch

# Reuse v2's per-turn tool sub-loop + cost/caching/compaction helpers verbatim,
# and v4's neutral wind-down / cadence helpers. We deliberately do NOT reuse the
# v4 context builder (it auto-injects AGENTS.md and focus-seeds recall).
from v2_session import (
    DEFAULT_COMPACTION_TOKENS,
    MAX_TICKS_PER_SESSION,
    _budget_pause_and_notify,
    _env,
    _estimate_tokens,
    _fatal_pause_and_notify,
    _install_signal_handlers,
    _system_blocks,
    compact_session_transcript,
    run_decay,
    run_one_tick,
)
from v4_session import (
    WIND_DOWN_NOTICE,
    _turn_is_substantive,
)

# Distress detection is language-only and identical to v3/v4. Reuse it.
from v3_session import _distress_check, _execute_side_effects

log = logging.getLogger("orchestrator.v5")

UTC = timezone.utc

MAX_TURNS_PER_SESSION = MAX_TICKS_PER_SESSION  # hard backstop


def _tools_for_call_v5(caching: bool) -> list[dict[str, Any]]:
    import copy
    spec = copy.deepcopy(TOOLS_SPEC_V5)
    if caching and spec:
        spec[-1]["cache_control"] = {"type": "ephemeral"}
    return spec


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
    cfg = instance.config

    # Step 0 — self-clear our cron entry.
    try:
        cron_control.remove_instance_entries(instance.id)
    except Exception:
        log.exception("Failed to remove cron entry; continuing")

    # Step 1 — lockfile.
    if not lockfile.acquire(instance.lock_path):
        held = lockfile.read_pid(instance.lock_path)
        log.warning("Another orchestrator running for %s (pid=%s); exiting", instance.id, held)
        return 0
    atexit.register(lockfile.release, instance.lock_path)

    os.environ.setdefault("HF_HOME", str(SHARED_HF_CACHE))
    episodic = EpisodicStore(instance.episodes_db)
    semantic = SemanticStore(instance.vectors_dir)

    box: dict[str, Any] = {"session_id": None}
    _install_signal_handlers(instance=instance, episodic=episodic, box=box)

    # clients
    try:
        slack_cfg = instance.config.get("slack") or {}
        slack: SlackClient | None = SlackClient(
            bot_token=_env("SLACK_BOT_TOKEN", required=True),
            ben_user_id=_env("SLACK_BEN_USER_ID", required=True),
            notes_channel=slack_cfg.get("notes_channel"),
            mirror_channel=slack_cfg.get("mirror_channel"),
            chat_channel=slack_cfg.get("chat_channel"),
        )
    except Exception:
        log.exception("Slack init failed; continuing without Slack")
        slack = None
    try:
        brave: BraveSearch | None = BraveSearch(_env("BRAVE_API_KEY", required=True))
    except Exception:
        log.exception("Brave init failed; web_search will error")
        brave = None
    anthropic_client = anthropic.Anthropic(api_key=_env("ANTHROPIC_API_KEY", required=True))

    # params (config over env defaults)
    model = instance.model
    max_tokens = int(cfg.get("max_tokens", 4096))
    caching = bool(cfg.get("prompt_caching", True))
    if is_openrouter_model(model):
        or_key = _env("OPENROUTER_API_KEY", required=True)
        client: Any = OpenRouterClient(api_key=or_key)
        caching = False  # prompt caching is Anthropic-specific
        log.info("Using OpenRouter model: %s (caching disabled)", model)
    else:
        client = anthropic_client
    compaction_on = bool(cfg.get("in_session_compaction", True))
    compaction_threshold = int(cfg.get("compaction_token_threshold", DEFAULT_COMPACTION_TOKENS))
    decay_hours = float(cfg.get("decay_hours", 72))
    daily_budget = float(cfg.get("daily_cost_cap_usd", _env("DAILY_BUDGET_USD", "50")))
    weekly_budget = float(cfg.get("weekly_cost_cap_usd", _env("WEEKLY_BUDGET_USD", "300")))
    session_cost_cap = float(cfg.get("session_cost_cap_usd", 20))
    max_wall = float(cfg.get("max_session_wall_clock_seconds", 14400))

    # circadian-style window params (same keys v3/v4 use)
    awake_min_lo = float(cfg.get("awake_minutes_min", 110))
    awake_min_hi = float(cfg.get("awake_minutes_max", 130))
    sleep_min_lo = float(cfg.get("sleep_minutes_min", 220))
    sleep_min_hi = float(cfg.get("sleep_minutes_max", 260))

    # adaptive cadence params (purely environmental; never disclosed)
    ACTIVE_GAP = float(cfg.get("cadence_active_gap_seconds", 10))
    IDLE_BASE = float(cfg.get("cadence_idle_base_seconds", 60))
    IDLE_CEIL = float(cfg.get("cadence_idle_ceil_seconds", 300))
    BACKOFF = float(cfg.get("cadence_backoff", 2.0))

    # registry last_wake
    try:
        with registry_txn() as reg:
            ent = reg.get("instances", {}).get(instance.id)
            if ent is not None:
                ent["last_wake"] = now_iso()
    except Exception:
        log.exception("Failed to update registry last_wake; continuing")

    # Step 2 — budget preflight.
    if episodic.cost_today() >= daily_budget:
        _budget_pause_and_notify(slack, episodic, instance_id=instance.id,
                                 reason=f"Daily cap reached: ${episodic.cost_today():.2f}/${daily_budget:.2f}")
        return 0
    if episodic.cost_last_7_days() >= weekly_budget:
        _budget_pause_and_notify(slack, episodic, instance_id=instance.id,
                                 reason=f"Weekly cap reached: ${episodic.cost_last_7_days():.2f}/${weekly_budget:.2f}")
        return 0

    # Step 3 — decay (unchanged from v2/v3/v4).
    decayed = run_decay(episodic, decay_hours)

    # Step 4 — session start + system-owned awake window.
    awake_min = random.uniform(awake_min_lo, awake_min_hi)
    wind_down_seconds = awake_min * 60.0

    invocation_num = episodic.next_invocation_num()
    session_id = episodic.start_session(invocation_num, pid=os.getpid())
    box["session_id"] = session_id
    started_at = now_iso()
    t_start = time.monotonic()
    log.info("v5 session %d started (invocation %d, pid %d); awake window %.1f min",
             session_id, invocation_num, os.getpid(), awake_min)

    # inbound chat from Ben (also used to seed the reload)
    last_seen = episodic.get_meta("last_seen_ben_dm_ts", "0") or "0"
    inbound_dms: list[dict[str, Any]] = []
    if slack is not None:
        inbound_dms = slack.fetch_dms_from_ben(oldest_ts=last_seen)
        for m in inbound_dms:
            episodic.log_ben_contact(invocation_num=invocation_num, direction="in", channel="dm", body=m["text"])
            last_seen = m["ts"]
    inbound_messages = len(inbound_dms)

    system_blocks = _system_blocks(build_v5_system_prompt(decay_hours=decay_hours), caching)
    tools = _tools_for_call_v5(caching)
    user0 = build_v5_session_context(
        instance=instance, episodic=episodic, semantic=semantic,
        decayed=decayed, inbound_ben_messages=[m["text"] for m in inbound_dms],
        decay_hours=decay_hours,
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": user0}]

    ctx = ToolContext(
        episodic=episodic, semantic=semantic, slack=slack, brave=brave,
        anthropic=anthropic_client, session_id=session_id, invocation_num=invocation_num,
        agent_root=instance.root.parent.parent, workspace_dir=instance.workspace_dir,
    )

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
        # budget guard
        if session_cost >= session_cost_cap:
            end_reason = "session_cost_cap"
            _budget_pause_and_notify(slack, episodic, instance_id=instance.id,
                                     reason=f"Session cap: ${session_cost:.2f}/${session_cost_cap:.2f}")
            break
        if episodic.cost_today() >= daily_budget:
            end_reason = "daily_cap"
            _budget_pause_and_notify(slack, episodic, instance_id=instance.id,
                                     reason=f"Daily cap: ${episodic.cost_today():.2f}/${daily_budget:.2f}")
            break
        if episodic.cost_last_7_days() >= weekly_budget:
            end_reason = "weekly_cap"
            _budget_pause_and_notify(slack, episodic, instance_id=instance.id,
                                     reason=f"Weekly cap: ${episodic.cost_last_7_days():.2f}/${weekly_budget:.2f}")
            break

        elapsed = time.monotonic() - t_start
        if elapsed >= max_wall:
            end_reason = "wall_clock_cap"
            log.warning("wall-clock cap reached (%.0fs); ending session", elapsed)
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
                client=client, model=model, max_tokens=max_tokens, caching=caching,
                system_blocks=system_blocks, messages=messages, tools=tools,
                ctx=ctx, episodic=episodic, session_id=session_id,
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
            episodic.add_cost(tokens_in=turn.tokens_in, tokens_out=turn.tokens_out, cost_usd=turn.cost_usd)
        except Exception:
            pass

        ts = turn.tick_state
        recent_states.append(ts)
        elapsed = time.monotonic() - t_start

        episodic.log_episode(
            invocation_num=invocation_num,
            current_focus=ts.get("tick_focus"),
            actions_taken=turn.actions,
            decisions_made=None,
            internal_state=ts.get("internal_state"),
            journal_entry=ts.get("journal_entry"),
            next_invoke_minutes=None,  # agent does not control scheduling in v5
            raw_output=json.dumps(ts, default=str),
            tokens_in=turn.tokens_in, tokens_out=turn.tokens_out, cost_usd=turn.cost_usd,
            session_id=session_id, wall_clock_seconds=elapsed,
        )

        _execute_side_effects(ts, slack, episodic, invocation_num)

        if slack:
            observer = format_episode_for_observer(
                invocation_num=invocation_num, timestamp=now_iso(),
                current_focus=ts.get("tick_focus"), actions=turn.actions,
                decisions=None, internal_state=ts.get("internal_state"),
                journal_entry=ts.get("journal_entry"),
                capability_request=ts.get("capability_request"),
                next_invoke_minutes=None,
                tokens_in=turn.tokens_in, tokens_out=turn.tokens_out,
                cost_usd=turn.cost_usd, parse_error=None,
            )
            if slack.post_to_observer_channel(observer):
                episodic.log_ben_contact(invocation_num=invocation_num, direction="out",
                                         channel="observer_channel", body=observer)

        # distress tripwire — NOTIFY + log, NEVER auto-pause.
        hit, excerpt = _distress_check(ts, recent_states)
        if hit:
            distress_alerts += 1
            alert = f"[distress monitor] turn {num_turns} — possible distress:\n{excerpt}"
            log.warning("v5 distress tripwire: %s", alert)
            if slack:
                try:
                    slack.dm_ben(alert)
                except Exception:
                    log.exception("failed to post distress alert")

        # WIND-DOWN: the only normal way the loop ends.
        if announced:
            break

        # Inbound chat: poll the chat channel. A new Ben message is activity
        # (snap to fast, reset backoff) and is injected as a user turn next loop.
        new_inbound: list[dict[str, Any]] = []
        if slack is not None:
            try:
                new_inbound = slack.fetch_dms_from_ben(oldest_ts=last_seen)
            except Exception:
                log.exception("inbound chat poll failed")
                new_inbound = []
        if new_inbound:
            for m in new_inbound:
                episodic.log_ben_contact(invocation_num=invocation_num, direction="in", channel="dm", body=m["text"])
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

        log.info("turn %d: note=%r substantive=%s tools=%d cost=$%.4f cache_r=%d "
                 "elapsed=%.0fs/%.0fs gap=%.0fs",
                 num_turns, ts.get("tick_focus"), substantive, turn.tool_calls,
                 turn.cost_usd, turn.cache_read, elapsed, wind_down_seconds, gap)

        if compaction_on and _estimate_tokens(messages) > compaction_threshold:
            compact_session_transcript(messages, client, model)

        if elapsed >= wind_down_seconds:
            messages.append({"role": "user", "content": WIND_DOWN_NOTICE})
            announced = True
            continue  # give the agent ONE last turn

        if gap > 0:
            time.sleep(gap)

    # Step 6 — finalize.
    ended_at = now_iso()
    elapsed = time.monotonic() - t_start
    sleep_min = random.uniform(sleep_min_lo, sleep_min_hi)

    try:
        episodic.update_session_stats(session_id, total_tool_calls=total_tool_calls, total_cost_usd=session_cost)
        episodic.end_session(session_id, status="killed" if fatal_error else "finished", end_reason=end_reason)
        # v5 reuses the v4_sessions record (identical session shape).
        episodic.log_v4_session(
            session_id=session_id, started_at=started_at, ended_at=ended_at,
            awake_seconds_target=wind_down_seconds, actual_awake_seconds=elapsed,
            scheduled_sleep_minutes=sleep_min, num_turns=num_turns,
            active_turns=active_turns, idle_turns=idle_turns,
            inbound_messages=inbound_messages, end_reason=end_reason,
            total_cost_usd=session_cost, decayed_count=len(decayed),
            consolidated_count=consolidated_count, distress_alerts=distress_alerts,
        )
    except Exception:
        log.exception("Failed to finalize session record")

    if inbound_dms:
        try:
            episodic.set_meta("last_seen_ben_dm_ts", last_seen)
        except Exception:
            pass
    elif slack is not None and last_seen != (episodic.get_meta("last_seen_ben_dm_ts", "0") or "0"):
        try:
            episodic.set_meta("last_seen_ben_dm_ts", last_seen)
        except Exception:
            pass

    if fatal_error:
        _fatal_pause_and_notify(
            slack, episodic, instance=instance, reason=f"session error: {fatal_error}"
        )
        log.error("v5 session %d ended on fatal error: %s", session_id, fatal_error)
        return 0

    log.info(
        "v5 session %d ended: reason=%s turns=%d (active=%d idle=%d) awake=%.1fm "
        "(planned %.1fm) inbound=%d cost=$%.4f cache_read=%d consolidated=%d "
        "decayed=%d distress_alerts=%d next_wake=%.1fm",
        session_id, end_reason, num_turns, active_turns, idle_turns, elapsed / 60,
        wind_down_seconds / 60, inbound_messages, session_cost, total_cache_read,
        consolidated_count, len(decayed), distress_alerts, sleep_min,
    )

    if slack:
        summary = (
            f":information_source: *v5 session #{invocation_num} ended* — {end_reason}\n"
            f"turns: {num_turns} (active {active_turns} · idle {idle_turns})  ·  "
            f"awake: {elapsed/60:.1f} min (planned {wind_down_seconds/60:.0f} min)\n"
            f"inbound: {inbound_messages}  ·  consolidated: {consolidated_count}  ·  "
            f"decayed at start: {len(decayed)}  ·  distress alerts: {distress_alerts}  ·  "
            f"cost: ${session_cost:.4f}  ·  next wake: {sleep_min:.0f} min"
        )
        slack.post_to_observer_channel(summary)

    # Step 7 — budget recheck + schedule next wake (NOT agent-controlled).
    if episodic.cost_today() >= daily_budget or episodic.cost_last_7_days() >= weekly_budget:
        _budget_pause_and_notify(slack, episodic, instance_id=instance.id,
                                 reason="post-session budget cap reached")
        return 0

    # Post-session research panel (inline, never blocks the next wake).
    try:
        from research.panel import run_research_panel
        from research.store import ResearchStore

        run_research_panel(
            instance=instance, episodic=episodic,
            research_store=ResearchStore(instance.episodes_db),
            anthropic_client=anthropic_client,
            session_id=session_id, invocation_num=invocation_num,
            semantic=semantic, agent_root=instance.root.parent.parent,
        )
    except Exception:
        log.exception("Research panel failed; continuing to schedule next wake")

    try:
        cron_control.install_instance_one_shot(instance.id, minutes_from_now=int(sleep_min))
        log.info("Next wake scheduled in %d min (rest period)", int(sleep_min))
    except Exception:
        log.exception("Failed to install cron entry")

    return 0
