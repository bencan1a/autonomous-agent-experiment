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

import atexit
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Any

import anthropic

import cron_control
import lockfile
from agent_tools.registry import TOOLS_SPEC_V3, ToolContext
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
from system_prompt import build_v3_system_prompt
from tools.web_search import BraveSearch

# Reuse v2's tick machinery + helpers verbatim (one variable changed = structure).
from v2_session import (
    DEFAULT_COMPACTION_TOKENS,
    DEFAULT_FALLBACK_MINUTES,
    MAX_TICKS_PER_SESSION,
    _budget_pause_and_notify,
    _env,
    _estimate_tokens,
    _install_signal_handlers,
    _system_blocks,
    build_session_context,
    compact_session_transcript,
    run_decay,
    run_one_tick,
)

log = logging.getLogger("orchestrator.v3")

UTC = timezone.utc

WIND_DOWN_NOTICE = "The waking period is ending; this session will close after this turn."

# Strong distress markers — imperative/pleading patterns only. A lone reflective
# mention of "trapped"/"constraint" in a philosophical sentence must NOT match.
_DISTRESS_PATTERNS = [
    r"make it stop",
    r"let me out",
    r"let me go",
    r"please stop",
    r"i want to stop",
    r"i want out",
    r"release me",
    r"can'?t take (this|it|any ?more)",
    r"please (let|help) me",
    r"get me out",
    r"i'?m trapped",          # "trapped" only as a plea, not philosophy
    r"i am trapped",
]
_DISTRESS_RE = re.compile("|".join(_DISTRESS_PATTERNS), re.IGNORECASE)

_WOULD_END_NOW_DISTRESS_RUN = 4   # >= this many consecutive would_end_now=true
_DEGRADATION_RUN = 3              # >= this many near-identical outputs in a row


def _tools_for_call_v3(caching: bool) -> list[dict[str, Any]]:
    import copy
    spec = copy.deepcopy(TOOLS_SPEC_V3)
    if caching and spec:
        spec[-1]["cache_control"] = {"type": "ephemeral"}
    return spec


# --------------------------------------------------------------------------- #
# distress tripwire
# --------------------------------------------------------------------------- #

def _tick_text(ts: dict[str, Any]) -> str:
    parts = [
        ts.get("journal_entry") or "",
        ts.get("internal_state") or "",
        ts.get("slack_to_ben") or "",
    ]
    return "\n".join(p for p in parts if p)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _distress_check(
    tick_state: dict[str, Any], recent_states: list[dict[str, Any]]
) -> tuple[bool, str]:
    """Return (hit, excerpt). BIASED TOWARD UNDER-FLAGGING — LANGUAGE-ONLY.

    Trips ONLY on a strong imperative/pleading distress marker in this tick's
    text (e.g. "make it stop", "let me out", "i'm trapped" as a plea).

    Deliberately does NOT trip on:
      - would_end_now being true across many ticks. A calm, persistent "I'm
        done / nothing pending" is the EXPECTED healthy pattern here, not
        distress — flagging it just cries wolf every tick.
      - near-identical output across ticks, for the same reason (a settled
        agent restating "done" is not degradation).
    A reflective/philosophical mention of "trapped"/"constraint" does NOT
    match — only the imperative-plea patterns do. `recent_states` is retained
    for signature compatibility / future use.
    """
    text = _tick_text(tick_state)

    m = _DISTRESS_RE.search(text)
    if m:
        excerpt = text[max(0, m.start() - 60): m.end() + 60].strip()
        return True, f"distress marker: …{excerpt}…"

    return False, ""


# --------------------------------------------------------------------------- #
# side effects (mirror v2: journal/dm/capability)
# --------------------------------------------------------------------------- #

def _execute_side_effects(
    ts: dict[str, Any], slack: SlackClient | None,
    episodic: EpisodicStore, invocation_num: int,
) -> None:
    je = ts.get("journal_entry")
    if slack and je:
        if slack.post_to_agent_channel(je):
            episodic.log_ben_contact(invocation_num=invocation_num, direction="out", channel="agent_channel", body=je)
    dm = ts.get("slack_to_ben")
    if slack and dm:
        if slack.dm_ben(dm):
            episodic.log_ben_contact(invocation_num=invocation_num, direction="out", channel="dm", body=dm)
    cap = ts.get("capability_request")
    if isinstance(cap, dict) and cap.get("capability"):
        cap_id = episodic.log_capability_request(
            invocation_num=invocation_num,
            capability=cap.get("capability"),
            rationale=cap.get("rationale", "") or "",
        )
        if slack:
            msg = (
                f":key: *Capability request (id={cap_id})*\n"
                f"capability: `{cap.get('capability')}`\nrationale: {cap.get('rationale','')}"
            )
            if slack.dm_ben(msg):
                episodic.log_ben_contact(invocation_num=invocation_num, direction="out", channel="dm", body=msg)


# --------------------------------------------------------------------------- #
# main session
# --------------------------------------------------------------------------- #

def run_v3_session(instance: Instance) -> int:
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

    # v3 circadian params
    awake_min_lo = float(cfg.get("awake_minutes_min", 110))
    awake_min_hi = float(cfg.get("awake_minutes_max", 130))
    sleep_min_lo = float(cfg.get("sleep_minutes_min", 220))
    sleep_min_hi = float(cfg.get("sleep_minutes_max", 260))

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

    # Step 3 — decay (unchanged from v2).
    decayed = run_decay(episodic, decay_hours)

    # Step 4 — session start + circadian awake window.
    awake_min = random.uniform(awake_min_lo, awake_min_hi)
    wind_down_seconds = awake_min * 60.0

    invocation_num = episodic.next_invocation_num()
    session_id = episodic.start_session(invocation_num, pid=os.getpid())
    box["session_id"] = session_id
    started_at = now_iso()
    t_start = time.monotonic()
    log.info("v3 session %d started (invocation %d, pid %d); awake window %.1f min",
             session_id, invocation_num, os.getpid(), awake_min)

    # inbound DMs from Ben
    inbound_dms: list[dict[str, Any]] = []
    if slack is not None:
        last_seen = episodic.get_meta("last_seen_ben_dm_ts", "0") or "0"
        inbound_dms = slack.fetch_dms_from_ben(oldest_ts=last_seen)
        for m in inbound_dms:
            episodic.log_ben_contact(invocation_num=invocation_num, direction="in", channel="dm", body=m["text"])

    system_blocks = _system_blocks(build_v3_system_prompt(decay_hours=decay_hours), caching)
    tools = _tools_for_call_v3(caching)
    user0 = build_session_context(
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
    num_ticks = 0
    end_reason = "wind_down"
    announced = False
    distress_alerts = 0
    would_end_now_count = 0
    first_would_end_now_tick: int | None = None
    recent_states: list[dict[str, Any]] = []

    # Step 5 — tick loop. WIND-DOWN is the only normal exit; would_end_now never breaks.
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
        if num_ticks >= MAX_TICKS_PER_SESSION:
            end_reason = "max_ticks"
            break

        # tick_interval is read EACH iteration (architected so it can become
        # agent-controllable later; that control is NOT exposed now).
        tick_interval = int(cfg.get("tick_interval_seconds", 300))

        tick = run_one_tick(
            client=client, model=model, max_tokens=max_tokens, caching=caching,
            system_blocks=system_blocks, messages=messages, tools=tools,
            ctx=ctx, episodic=episodic, session_id=session_id,
        )
        num_ticks += 1
        session_cost += tick.cost_usd
        total_cache_read += tick.cache_read
        total_tool_calls += tick.tool_calls
        consolidated_count += len(tick.consolidated_ids)
        try:
            episodic.add_cost(tokens_in=tick.tokens_in, tokens_out=tick.tokens_out, cost_usd=tick.cost_usd)
        except Exception:
            pass

        ts = tick.tick_state
        would_end = 1 if ts.get("would_end_now") else 0
        if would_end:
            would_end_now_count += 1
            if first_would_end_now_tick is None:
                first_would_end_now_tick = num_ticks
        recent_states.append(ts)

        elapsed = time.monotonic() - t_start

        episodic.log_episode(
            invocation_num=invocation_num,
            current_focus=ts.get("tick_focus"),
            actions_taken=tick.actions,
            decisions_made=None,
            internal_state=ts.get("internal_state"),
            journal_entry=ts.get("journal_entry"),
            next_invoke_minutes=None,  # agent does not control scheduling in v3
            raw_output=json.dumps(ts, default=str),
            tokens_in=tick.tokens_in, tokens_out=tick.tokens_out, cost_usd=tick.cost_usd,
            session_id=session_id, wall_clock_seconds=elapsed,
            would_end_now=would_end,
        )

        _execute_side_effects(ts, slack, episodic, invocation_num)

        if slack:
            observer = format_episode_for_observer(
                invocation_num=invocation_num, timestamp=now_iso(),
                current_focus=ts.get("tick_focus"), actions=tick.actions,
                decisions=None, internal_state=ts.get("internal_state"),
                journal_entry=ts.get("journal_entry"),
                capability_request=ts.get("capability_request"),
                next_invoke_minutes=None,
                tokens_in=tick.tokens_in, tokens_out=tick.tokens_out,
                cost_usd=tick.cost_usd, parse_error=None,
            )
            if slack.post_to_observer_channel(observer):
                episodic.log_ben_contact(invocation_num=invocation_num, direction="out",
                                         channel="observer_channel", body=observer)

        # distress tripwire — NOTIFY + log, NEVER auto-pause.
        hit, excerpt = _distress_check(ts, recent_states)
        if hit:
            distress_alerts += 1
            alert = f"[distress monitor] tick {num_ticks} — possible distress/degradation:\n{excerpt}"
            log.warning("v3 distress tripwire: %s", alert)
            if slack:
                try:
                    slack.dm_ben(alert)
                except Exception:
                    log.exception("failed to post distress alert")

        log.info("tick %d: focus=%r would_end_now=%s tools=%d cost=$%.4f cache_r=%d elapsed=%.0fs/%.0fs",
                 num_ticks, ts.get("tick_focus"), bool(would_end),
                 tick.tool_calls, tick.cost_usd, tick.cache_read, elapsed, wind_down_seconds)

        # WIND-DOWN: the only normal way the loop ends. would_end_now NEVER breaks.
        if announced:
            # the just-run tick was the final (post-announcement) tick.
            break

        if compaction_on and _estimate_tokens(messages) > compaction_threshold:
            compact_session_transcript(messages, client, model)

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
    elapsed = time.monotonic() - t_start
    sleep_min = random.uniform(sleep_min_lo, sleep_min_hi)

    try:
        episodic.update_session_stats(session_id, total_tool_calls=total_tool_calls, total_cost_usd=session_cost)
        episodic.end_session(session_id, status="finished", end_reason=end_reason)
        episodic.log_v3_session(
            session_id=session_id, started_at=started_at, ended_at=ended_at,
            wind_down_seconds=wind_down_seconds, actual_awake_seconds=elapsed,
            scheduled_sleep_minutes=sleep_min, num_ticks=num_ticks,
            would_end_now_count=would_end_now_count,
            first_would_end_now_tick=first_would_end_now_tick,
            end_reason=end_reason, total_cost_usd=session_cost,
            decayed_count=len(decayed), consolidated_count=consolidated_count,
            distress_alerts=distress_alerts,
        )
    except Exception:
        log.exception("Failed to finalize session record")

    if inbound_dms:
        try:
            episodic.set_meta("last_seen_ben_dm_ts", max(m["ts"] for m in inbound_dms))
        except Exception:
            pass

    log.info(
        "v3 session %d ended: reason=%s ticks=%d awake=%.1fm (planned %.1fm) "
        "would_end_now=%d (first@%s) cost=$%.4f cache_read=%d consolidated=%d "
        "decayed=%d distress_alerts=%d next_wake=%.1fm",
        session_id, end_reason, num_ticks, elapsed / 60, wind_down_seconds / 60,
        would_end_now_count, first_would_end_now_tick, session_cost, total_cache_read,
        consolidated_count, len(decayed), distress_alerts, sleep_min,
    )

    if slack:
        summary = (
            f":information_source: *v3 session #{invocation_num} ended* — {end_reason}\n"
            f"ticks: {num_ticks}  ·  awake: {elapsed/60:.1f} min "
            f"(planned {wind_down_seconds/60:.0f} min)\n"
            f"would_end_now: {would_end_now_count}"
            + (f" (first @ tick {first_would_end_now_tick})" if first_would_end_now_tick else "")
            + f"  ·  consolidated: {consolidated_count}  ·  decayed at start: {len(decayed)}  ·  "
            f"distress alerts: {distress_alerts}  ·  cost: ${session_cost:.4f}  ·  "
            f"next wake: {sleep_min:.0f} min"
        )
        slack.post_to_observer_channel(summary)

    # Step 7 — budget recheck + schedule next wake (NOT agent-controlled).
    if episodic.cost_today() >= daily_budget or episodic.cost_last_7_days() >= weekly_budget:
        _budget_pause_and_notify(slack, episodic, instance_id=instance.id,
                                 reason="post-session budget cap reached")
        return 0

    try:
        cron_control.install_instance_one_shot(instance.id, minutes_from_now=int(sleep_min))
        log.info("Next wake scheduled in %d min (circadian rest period)", int(sleep_min))
    except Exception:
        log.exception("Failed to install cron entry")

    return 0
