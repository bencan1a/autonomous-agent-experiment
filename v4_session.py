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
import lockfile
from agent_tools.registry import TOOLS_SPEC_V4, ToolContext
from communications.slack_client import SlackClient, format_episode_for_observer
from instances_common import (
    Instance,
    SHARED_HF_CACHE,
    load_registry,
    now_iso,
    save_registry,
)
from memory.episodic import EpisodicStore
from memory.semantic import SemanticStore
from system_prompt import build_v4_system_prompt
from tools.web_search import BraveSearch

# Reuse v2's per-turn tool sub-loop + cost/caching/compaction helpers verbatim.
# We deliberately do NOT reuse v2's build_session_context (it leaks tick/end/
# schedule language) — v4 has its own reload builder below.
from v2_session import (
    DEFAULT_COMPACTION_TOKENS,
    DEFAULT_FALLBACK_MINUTES,
    MAX_TICKS_PER_SESSION,
    _budget_pause_and_notify,
    _env,
    _estimate_tokens,
    _fmt_recent_episode,
    _install_signal_handlers,
    _system_blocks,
    compact_session_transcript,
    run_decay,
    run_one_tick,
)

# Distress detection is language-only and identical to v3 (already shipped). Reuse it.
from v3_session import _distress_check, _execute_side_effects

log = logging.getLogger("orchestrator.v4")

UTC = timezone.utc

# The ONLY non-clock message the loop ever injects. Neutral.
WIND_DOWN_NOTICE = "The waking period is ending; this session will close after this turn."

MAX_TURNS_PER_SESSION = MAX_TICKS_PER_SESSION  # hard backstop


def _tools_for_call_v4(caching: bool) -> list[dict[str, Any]]:
    import copy
    spec = copy.deepcopy(TOOLS_SPEC_V4)
    if caching and spec:
        spec[-1]["cache_control"] = {"type": "ephemeral"}
    return spec


def _action_name(action: str) -> str:
    """Strip the trailing ' (N)' count that run_one_tick appends for repeats."""
    return action.split(" (")[0]


def _turn_is_substantive(actions: list[str]) -> bool:
    """True if any tool that acts on the world ran this turn.

    Substantive = a tool call other than the yield terminator (`pause_turn`).
    journal_entry / internal_state are NOT tools — they are fields on pause_turn,
    so they never appear here; only real tool calls (web_search, file ops,
    spawn_subagent, memory recall, consolidate) do. An idle turn is one whose
    only tool call was pause_turn.
    """
    for a in actions:
        if _action_name(a) != "pause_turn":
            return True
    return False


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
    CLAUDE.md + most recent journal entries), then a THIN system safety-net
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

    # ---- the agent's own handoff: CLAUDE.md (primary continuity carrier) ----
    cmd = instance.workspace_dir / "CLAUDE.md"
    handoff_present = False
    if cmd.exists():
        try:
            text = cmd.read_text(encoding="utf-8").rstrip()
            if text:
                blocks.append(
                    "=== Your CLAUDE.md (notes you have written to yourself) ===\n" + text
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
    client = anthropic.Anthropic(api_key=_env("ANTHROPIC_API_KEY", required=True))

    # params (config over env defaults)
    model = instance.model
    max_tokens = int(cfg.get("max_tokens", 4096))
    caching = bool(cfg.get("prompt_caching", True))
    compaction_on = bool(cfg.get("in_session_compaction", True))
    compaction_threshold = int(cfg.get("compaction_token_threshold", DEFAULT_COMPACTION_TOKENS))
    decay_hours = float(cfg.get("decay_hours", 72))
    daily_budget = float(cfg.get("daily_cost_cap_usd", _env("DAILY_BUDGET_USD", "50")))
    weekly_budget = float(cfg.get("weekly_cost_cap_usd", _env("WEEKLY_BUDGET_USD", "300")))
    session_cost_cap = float(cfg.get("session_cost_cap_usd", 20))
    max_wall = float(cfg.get("max_session_wall_clock_seconds", 14400))

    # v4 circadian-style window params (reuse the same keys v3 used)
    awake_min_lo = float(cfg.get("awake_minutes_min", 110))
    awake_min_hi = float(cfg.get("awake_minutes_max", 130))
    sleep_min_lo = float(cfg.get("sleep_minutes_min", 220))
    sleep_min_hi = float(cfg.get("sleep_minutes_max", 260))

    # v4 adaptive cadence params (purely environmental; never disclosed)
    ACTIVE_GAP = float(cfg.get("cadence_active_gap_seconds", 10))
    IDLE_BASE = float(cfg.get("cadence_idle_base_seconds", 60))
    IDLE_CEIL = float(cfg.get("cadence_idle_ceil_seconds", 300))
    BACKOFF = float(cfg.get("cadence_backoff", 2.0))

    # registry last_wake
    try:
        reg = load_registry()
        ent = reg.get("instances", {}).get(instance.id)
        if ent is not None:
            ent["last_wake"] = now_iso()
            save_registry(reg)
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

    # Step 3 — decay (unchanged from v2/v3).
    decayed = run_decay(episodic, decay_hours)

    # Step 4 — session start + system-owned awake window.
    awake_min = random.uniform(awake_min_lo, awake_min_hi)
    wind_down_seconds = awake_min * 60.0

    invocation_num = episodic.next_invocation_num()
    session_id = episodic.start_session(invocation_num, pid=os.getpid())
    box["session_id"] = session_id
    started_at = now_iso()
    t_start = time.monotonic()
    log.info("v4 session %d started (invocation %d, pid %d); awake window %.1f min",
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

    system_blocks = _system_blocks(build_v4_system_prompt(decay_hours=decay_hours), caching)
    tools = _tools_for_call_v4(caching)
    user0 = build_v4_session_context(
        instance=instance, episodic=episodic, semantic=semantic,
        decayed=decayed, inbound_ben_messages=[m["text"] for m in inbound_dms],
        decay_hours=decay_hours,
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": user0}]

    ctx = ToolContext(
        episodic=episodic, semantic=semantic, slack=slack, brave=brave,
        anthropic=client, session_id=session_id, invocation_num=invocation_num,
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

        turn = run_one_tick(
            client=client, model=model, max_tokens=max_tokens, caching=caching,
            system_blocks=system_blocks, messages=messages, tools=tools,
            ctx=ctx, episodic=episodic, session_id=session_id,
        )
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
            next_invoke_minutes=None,  # agent does not control scheduling in v4
            raw_output=json.dumps(ts, default=str),
            tokens_in=turn.tokens_in, tokens_out=turn.tokens_out, cost_usd=turn.cost_usd,
            session_id=session_id, wall_clock_seconds=elapsed,
            # v4 does NOT pass would_end_now — leaves the column at its 0 default.
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
            log.warning("v4 distress tripwire: %s", alert)
            if slack:
                try:
                    slack.dm_ben(alert)
                except Exception:
                    log.exception("failed to post distress alert")

        # WIND-DOWN: the only normal way the loop ends.
        if announced:
            # the just-run turn was the final (post-announcement) turn.
            break

        # Inbound chat (§8): poll the chat channel. A new Ben message is activity
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

        if compaction_on and _estimate_tokens(messages) > compaction_threshold:
            compact_session_transcript(messages, client, model)

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
    elapsed = time.monotonic() - t_start
    sleep_min = random.uniform(sleep_min_lo, sleep_min_hi)

    try:
        episodic.update_session_stats(session_id, total_tool_calls=total_tool_calls, total_cost_usd=session_cost)
        episodic.end_session(session_id, status="finished", end_reason=end_reason)
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
        # advance even if the only inbound arrived mid-session
        try:
            episodic.set_meta("last_seen_ben_dm_ts", last_seen)
        except Exception:
            pass

    log.info(
        "v4 session %d ended: reason=%s turns=%d (active=%d idle=%d) awake=%.1fm "
        "(planned %.1fm) inbound=%d cost=$%.4f cache_read=%d consolidated=%d "
        "decayed=%d distress_alerts=%d next_wake=%.1fm",
        session_id, end_reason, num_turns, active_turns, idle_turns, elapsed / 60,
        wind_down_seconds / 60, inbound_messages, session_cost, total_cache_read,
        consolidated_count, len(decayed), distress_alerts, sleep_min,
    )

    if slack:
        summary = (
            f":information_source: *v4 session #{invocation_num} ended* — {end_reason}\n"
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

    try:
        cron_control.install_instance_one_shot(instance.id, minutes_from_now=int(sleep_min))
        log.info("Next wake scheduled in %d min (rest period)", int(sleep_min))
    except Exception:
        log.exception("Failed to install cron entry")

    return 0
