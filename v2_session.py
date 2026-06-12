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

import atexit
import json
import logging
import os
import signal
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import anthropic

import cron_control
import lockfile
from agent_tools.registry import TOOLS_SPEC_V2, ToolContext, dispatch
from claude_client import _pricing_for
from communications.slack_client import SlackClient, format_episode_for_observer
from instances_common import (
    Instance,
    SHARED_HF_CACHE,
    now_iso,
    registry_txn,
)
from memory.episodic import EpisodicStore
from memory.semantic import SemanticStore
from system_prompt import build_v2_system_prompt
from tools.web_search import BraveSearch

log = logging.getLogger("orchestrator.v2")

UTC = timezone.utc
TOOL_RESULT_TRUNC = 6000
MAX_TICK_TOOL_CALLS = 40        # safety: tool calls within a single tick
MAX_TICKS_PER_SESSION = 400     # safety: hard ceiling on ticks
DEFAULT_FALLBACK_MINUTES = 60
DEFAULT_COMPACTION_TOKENS = 120_000


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #

def _turn_cost(
    model: str, *, input_tokens: int, output_tokens: int,
    cache_read: int, cache_creation: int,
) -> float:
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
# decay
# --------------------------------------------------------------------------- #

def run_decay(episodic: EpisodicStore, decay_hours: float) -> list[dict[str, Any]]:
    """Delete un-consolidated episodes past the decay horizon. Returns what was
    deleted (brief dicts) so the agent can be told, factually, what is now gone."""
    stale = episodic.unconsolidated_older_than_hours(decay_hours)
    if not stale:
        return []
    brief = [
        {
            "invocation_num": e.get("invocation_num"),
            "timestamp": e.get("timestamp"),
            "current_focus": e.get("current_focus"),
        }
        for e in stale
    ]
    episodic.delete_episodes([e["id"] for e in stale])
    log.info("decay: deleted %d un-consolidated episode(s) past %sh", len(stale), decay_hours)
    return brief


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

    # CLAUDE.md
    cmd = instance.workspace_dir / "CLAUDE.md"
    if cmd.exists():
        try:
            text = cmd.read_text(encoding="utf-8").rstrip()
            if text:
                blocks.append("=== Your CLAUDE.md (notes you have written to yourself) ===\n" + text)
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


def _tools_for_call(caching: bool) -> list[dict[str, Any]]:
    import copy
    spec = copy.deepcopy(TOOLS_SPEC_V2)
    if caching and spec:
        spec[-1]["cache_control"] = {"type": "ephemeral"}
    return spec


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
        res.cost_usd += _turn_cost(model, input_tokens=tin, output_tokens=tout, cache_read=cr, cache_creation=cc)

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
# side effects
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


def _budget_pause_and_notify(
    slack: SlackClient | None, episodic: EpisodicStore, *, instance_id: str, reason: str,
) -> None:
    log.warning("BUDGET PAUSE: %s", reason)
    cron_control.clear_instance(instance_id)
    msg = (
        ":octagonal_sign: *v2 agent paused — budget exceeded.*\n"
        f"{reason}\nCron entry cleared. Re-enable manually after review."
    )
    if slack:
        slack.post_to_observer_channel(msg)
        if slack.dm_ben(msg):
            episodic.log_ben_contact(invocation_num=None, direction="out", channel="dm", body=msg)


def _install_signal_handlers(*, instance: Instance, episodic: EpisodicStore, box: dict[str, Any]) -> None:
    def handler(signum: int, _frame: Any) -> None:
        log.warning("Received signal %d; cleaning up", signum)
        sid = box.get("session_id")
        if sid is not None:
            try:
                episodic.end_session(sid, status="killed", end_reason=f"signal_{signum}")
            except Exception:
                pass
        try:
            lockfile.release(instance.lock_path)
        except Exception:
            pass
        try:
            cron_control.install_instance_one_shot(instance.id, minutes_from_now=DEFAULT_FALLBACK_MINUTES)
        except Exception:
            pass
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


# --------------------------------------------------------------------------- #
# main session
# --------------------------------------------------------------------------- #

def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val  # type: ignore[return-value]


def run_v2_session(instance: Instance) -> int:
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
    min_wake_hours = float(cfg.get("min_wake_hours", 2))
    min_wake_seconds = min_wake_hours * 3600
    tick_interval = int(cfg.get("tick_interval_seconds", 300))
    decay_hours = float(cfg.get("decay_hours", 72))
    daily_budget = float(cfg.get("daily_cost_cap_usd", _env("DAILY_BUDGET_USD", "50")))
    weekly_budget = float(cfg.get("weekly_cost_cap_usd", _env("WEEKLY_BUDGET_USD", "300")))
    session_cost_cap = float(cfg.get("session_cost_cap_usd", 20))
    max_wall = float(cfg.get("max_session_wall_clock_seconds", 14400))
    min_interval_minutes = int(cfg.get("min_interval_minutes", 30))

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

    # Step 3 — decay.
    decayed = run_decay(episodic, decay_hours)

    # Step 4 — session start.
    invocation_num = episodic.next_invocation_num()
    session_id = episodic.start_session(invocation_num, pid=os.getpid())
    box["session_id"] = session_id
    started_at = now_iso()
    t_start = time.monotonic()
    log.info("v2 session %d started (invocation %d, pid %d)", session_id, invocation_num, os.getpid())

    # inbound DMs from Ben
    inbound_dms: list[dict[str, Any]] = []
    if slack is not None:
        last_seen = episodic.get_meta("last_seen_ben_dm_ts", "0") or "0"
        inbound_dms = slack.fetch_dms_from_ben(oldest_ts=last_seen)
        for m in inbound_dms:
            episodic.log_ben_contact(invocation_num=invocation_num, direction="in", channel="dm", body=m["text"])

    system_blocks = _system_blocks(
        build_v2_system_prompt(min_wake_hours=min_wake_hours, decay_hours=decay_hours), caching
    )
    tools = _tools_for_call(caching)
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
    end_reason = "agent_ended"
    next_invoke_minutes: int | None = None

    # Step 5 — tick loop.
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
        elapsed = time.monotonic() - t_start

        episode_id = episodic.log_episode(
            invocation_num=invocation_num,
            current_focus=ts.get("tick_focus"),
            actions_taken=tick.actions,
            decisions_made=None,
            internal_state=ts.get("internal_state"),
            journal_entry=ts.get("journal_entry"),
            next_invoke_minutes=ts.get("next_invoke_minutes") if ts.get("end_session") else None,
            raw_output=json.dumps(ts, default=str),
            tokens_in=tick.tokens_in, tokens_out=tick.tokens_out, cost_usd=tick.cost_usd,
            session_id=session_id, wall_clock_seconds=elapsed,
        )

        _execute_side_effects(ts, slack, episodic, invocation_num)

        if slack:
            observer = format_episode_for_observer(
                invocation_num=invocation_num, timestamp=now_iso(),
                current_focus=ts.get("tick_focus"), actions=tick.actions,
                decisions=None, internal_state=ts.get("internal_state"),
                journal_entry=ts.get("journal_entry"),
                capability_request=ts.get("capability_request"),
                next_invoke_minutes=ts.get("next_invoke_minutes") if ts.get("end_session") else None,
                tokens_in=tick.tokens_in, tokens_out=tick.tokens_out,
                cost_usd=tick.cost_usd, parse_error=None,
            )
            if slack.post_to_observer_channel(observer):
                episodic.log_ben_contact(invocation_num=invocation_num, direction="out",
                                         channel="observer_channel", body=observer)

        log.info("tick %d: focus=%r end_session=%s tools=%d cost=$%.4f cache_r=%d",
                 num_ticks, ts.get("tick_focus"), ts.get("end_session"),
                 tick.tool_calls, tick.cost_usd, tick.cache_read)

        if ts.get("end_session"):
            end_reason = "agent_ended"
            next_invoke_minutes = ts.get("next_invoke_minutes")
            break

        if compaction_on and _estimate_tokens(messages) > compaction_threshold:
            compact_session_transcript(messages, client, model)

        if tick_interval > 0:
            time.sleep(tick_interval)

    # Step 6 — finalize.
    ended_at = now_iso()
    elapsed = time.monotonic() - t_start
    ended_early = bool(end_reason == "agent_ended" and elapsed < min_wake_seconds)

    if isinstance(next_invoke_minutes, (int, float)):
        nim = int(next_invoke_minutes)
        if nim < min_interval_minutes:
            log.warning("next_invoke_minutes=%d clamped to %d", nim, min_interval_minutes)
            nim = min_interval_minutes
        next_invoke_minutes = nim
    else:
        next_invoke_minutes = None

    try:
        episodic.update_session_stats(session_id, total_tool_calls=total_tool_calls, total_cost_usd=session_cost)
        episodic.end_session(session_id, status="finished", end_reason=end_reason)
        episodic.log_v2_session(
            session_id=session_id, started_at=started_at, ended_at=ended_at,
            num_ticks=num_ticks, elapsed_seconds=elapsed, min_wake_seconds=min_wake_seconds,
            ended_early=ended_early, end_reason=end_reason, next_invoke_minutes=next_invoke_minutes,
            decayed_count=len(decayed), consolidated_count=consolidated_count,
        )
    except Exception:
        log.exception("Failed to finalize session record")

    if inbound_dms:
        try:
            episodic.set_meta("last_seen_ben_dm_ts", max(m["ts"] for m in inbound_dms))
        except Exception:
            pass

    log.info(
        "v2 session %d ended: reason=%s ticks=%d elapsed=%.1fm min_wake=%.1fm early=%s "
        "cost=$%.4f cache_read=%d consolidated=%d decayed=%d next_invoke=%s",
        session_id, end_reason, num_ticks, elapsed / 60, min_wake_seconds / 60,
        ended_early, session_cost, total_cache_read, consolidated_count, len(decayed),
        next_invoke_minutes,
    )

    if slack:
        summary = (
            f":information_source: *v2 session #{invocation_num} ended* — {end_reason}\n"
            f"ticks: {num_ticks}  ·  awake: {elapsed/60:.1f} min "
            f"(reference {min_wake_seconds/60:.0f} min){' · ended before reference' if ended_early else ''}\n"
            f"consolidated: {consolidated_count}  ·  decayed at start: {len(decayed)}  ·  "
            f"cost: ${session_cost:.4f}  ·  next wake: "
            + (f"{next_invoke_minutes} min" if next_invoke_minutes else "none")
        )
        slack.post_to_observer_channel(summary)

    # Step 7 — budget recheck + schedule next wake.
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
            anthropic_client=client,
            session_id=session_id, invocation_num=invocation_num,
            semantic=semantic, agent_root=instance.root.parent.parent,
        )
    except Exception:
        log.exception("Research panel failed; continuing to schedule next wake")

    if next_invoke_minutes is None:
        log.info("Agent chose not to reschedule. No cron entry installed.")
        cron_control.clear_instance(instance.id)
    else:
        try:
            cron_control.install_instance_one_shot(instance.id, minutes_from_now=next_invoke_minutes)
            log.info("Next wake scheduled in %d min", next_invoke_minutes)
        except Exception:
            log.exception("Failed to install cron entry")

    return 0
