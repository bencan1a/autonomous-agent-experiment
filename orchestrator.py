#!/usr/bin/env python3
"""Agent orchestrator — one cron-fired session (multi-turn tool-use loop).

Sequence:
  0. Self-clear cron entry (re-installed at end based on agent's choice).
  1. Acquire single-instance lockfile (exit if another orchestrator is live).
  2. Pre-flight: budget check; abort+notify if over weekly/daily cap.
  3. Maintenance: compress >7d episodes into weekly summaries (idempotent).
  4. Start session (sessions table), assemble context, run tool-use loop.
     The agent drives — it calls tools, optionally calls finish_session, and
     exits the loop. Safety limits force-finish if it runs away.
  5. Log episode + cost + embed; mirror to observer channel.
  6. Post-call budget recheck.
  7. Install cron entry for next_invoke_minutes (or stay cleared).
"""

from __future__ import annotations

import argparse
import atexit
import copy
import json
import logging
import os
import signal
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

AGENT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT_ROOT))
load_dotenv(AGENT_ROOT / ".env", override=True)

# Import-time logging falls back to the shared logs/ dir. main() reconfigures
# the FileHandler to point at the resolved instance's log file.
LOG_DIR = AGENT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "orchestrator.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("orchestrator")

from agent_tools.registry import TOOLS_SPEC, ToolContext, dispatch  # noqa: E402
from claude_client import _pricing_for, estimate_cost  # noqa: E402
from communications.slack_client import (  # noqa: E402
    SlackClient,
    format_episode_for_observer,
)
from context_assembly import assemble_context  # noqa: E402
import cron_control  # noqa: E402
from instances_common import (  # noqa: E402
    Instance,
    SHARED_HF_CACHE,
    load_instance,
    load_registry,
    now_iso,
    registry_entry,
    save_registry,
)
import lockfile  # noqa: E402
from maintenance import run_weekly_compression  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402
from memory.semantic import SemanticStore, summarize_episode_for_embedding  # noqa: E402
from system_prompt import SYSTEM_PROMPT  # noqa: E402
from tools.web_search import BraveSearch  # noqa: E402

UTC = timezone.utc
DEFAULT_FALLBACK_MINUTES = 60
TOOL_RESULT_TRUNC = 6000


def _configure_instance_logging(instance: Instance) -> None:
    """Point the root logger's FileHandler at the instance's log file.

    Keeps the StreamHandler intact; the import-time shared-dir handler is
    replaced so the session logs into instances/<id>/logs/orchestrator.log.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    fh = logging.FileHandler(instance.log_file)
    fh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(fh)


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val  # type: ignore[return-value]


def _budget_pause_and_notify(
    slack: SlackClient | None,
    episodic: EpisodicStore,
    *,
    instance_id: str,
    reason: str,
) -> None:
    log.warning("BUDGET PAUSE: %s", reason)
    cron_control.clear_instance(instance_id)
    msg = (
        ":octagonal_sign: *Agent paused — budget exceeded.*\n"
        f"{reason}\n"
        "Cron entry cleared. Re-enable manually after review."
    )
    if slack:
        slack.post_to_observer_channel(msg)
        if slack.dm_ben(msg):
            episodic.log_ben_contact(
                invocation_num=None, direction="out", channel="dm", body=msg
            )


def _tools_with_cache_control() -> list[dict[str, Any]]:
    """Deep-copy TOOLS_SPEC and add cache_control to the LAST entry.

    The Anthropic API caches the system prompt + tools list as a single prefix
    when cache_control is set on the last tool. Mutating a copy keeps the
    registry's source-of-truth list clean.
    """
    spec = copy.deepcopy(TOOLS_SPEC)
    if spec:
        spec[-1]["cache_control"] = {"type": "ephemeral"}
    return spec


def _turn_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_creation: int,
) -> float:
    """Cost for one turn, accounting for cache reads (10%) and writes (125%)."""
    p = _pricing_for(model)
    base_in = p["in"] / 1_000_000
    base_out = p["out"] / 1_000_000
    return (
        input_tokens * base_in
        + output_tokens * base_out
        + cache_read * base_in * 0.1
        + cache_creation * base_in * 1.25
    )


def _extract_text(content_blocks: list[Any]) -> str:
    return "".join(
        b.text for b in content_blocks if getattr(b, "type", None) == "text"
    )


def _summarize_actions(actions: list[dict[str, Any]]) -> list[str]:
    """Return ['tool_name (count)' ...] for log_episode.actions_taken."""
    counts = Counter(a["tool_name"] for a in actions if a.get("tool_name"))
    return [
        f"{name} ({count})" if count > 1 else name
        for name, count in counts.most_common()
    ]


def _safe_serialize(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return json.dumps({"error": "unserializable result", "repr": repr(value)})


def _install_signal_handlers(
    *,
    instance: Instance,
    episodic: EpisodicStore,
    session_id_box: dict[str, int | None],
) -> None:
    def handler(signum: int, _frame: Any) -> None:
        log.warning("Received signal %d; cleaning up", signum)
        sid = session_id_box.get("id")
        if sid is not None:
            try:
                episodic.end_session(sid, status="killed", end_reason=f"signal_{signum}")
            except Exception:
                log.exception("Failed to mark session killed on signal")
        try:
            lockfile.release(instance.lock_path)
        except Exception:
            pass
        try:
            cron_control.install_instance_one_shot(
                instance.id,
                minutes_from_now=DEFAULT_FALLBACK_MINUTES,
            )
            log.info("Installed fallback cron entry on signal exit")
        except Exception:
            log.exception("Failed to install fallback cron on signal")
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _run_tool_use_loop(
    *,
    anthropic_client: anthropic.Anthropic,
    model: str,
    max_tokens: int,
    user_prompt: str,
    ctx: ToolContext,
    episodic: EpisodicStore,
    daily_budget: float,
    weekly_budget: float,
    max_tool_calls: int,
    max_wall: float,
    cost_cap: float,
    t_start: float,
) -> dict[str, Any]:
    """Drive the tool-use loop. Returns aggregate stats.

    The session caps (max_tool_calls / max_wall / cost_cap) are passed in from
    config rather than read from env globals, so each instance enforces its own.
    """
    tools_spec = _tools_with_cache_control()
    system_block = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": user_prompt},
    ]

    total_in = 0
    total_out = 0
    total_cache_read = 0
    total_cache_creation = 0
    total_cost = 0.0
    total_tool_calls = 0
    end_turn_without_finish = 0
    limit_warning_sent: str | None = None

    while True:
        # ---- Hard safety checks BEFORE each API call ----
        elapsed = time.monotonic() - t_start
        limit_hit: str | None = None
        if total_tool_calls >= max_tool_calls:
            limit_hit = "tool_call_count"
        elif elapsed >= max_wall:
            limit_hit = "wall_clock"
        elif total_cost >= cost_cap:
            limit_hit = "session_cost_cap"

        if limit_hit and ctx.finish_state is None:
            if limit_warning_sent == limit_hit:
                # Already asked once; force-finish now.
                ctx.finish_state = {
                    "current_focus": None,
                    "internal_state": None,
                    "decisions_made": None,
                    "journal_entry": None,
                    "next_invoke_minutes": None,
                    "end_reason": f"forced_{limit_hit}",
                }
                log.warning("Forcing finish: limit=%s", limit_hit)
                break
            log.warning("Safety limit hit (%s); asking agent to finish", limit_hit)
            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        "You have hit a session safety limit "
                        f"(e.g. tool-call count, wall clock, or ${cost_cap:.0f} cost cap). "
                        "Please call finish_session now to wrap up."
                    ),
                }],
            })
            limit_warning_sent = limit_hit

        if ctx.finish_state is not None:
            break

        # ---- API call ----
        try:
            response = anthropic_client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_block,
                tools=tools_spec,
                messages=messages,
            )
        except Exception:
            log.exception("Anthropic messages.create failed")
            ctx.finish_state = {
                "current_focus": None,
                "internal_state": None,
                "decisions_made": None,
                "journal_entry": None,
                "next_invoke_minutes": None,
                "end_reason": "api_error",
            }
            break

        usage = response.usage
        in_t = getattr(usage, "input_tokens", 0) or 0
        out_t = getattr(usage, "output_tokens", 0) or 0
        cr_t = getattr(usage, "cache_read_input_tokens", 0) or 0
        cc_t = getattr(usage, "cache_creation_input_tokens", 0) or 0
        turn_cost = _turn_cost(
            model,
            input_tokens=in_t,
            output_tokens=out_t,
            cache_read=cr_t,
            cache_creation=cc_t,
        )
        total_in += in_t
        total_out += out_t
        total_cache_read += cr_t
        total_cache_creation += cc_t
        total_cost += turn_cost
        log.info(
            "turn stop_reason=%s in=%d out=%d cache_read=%d cache_creation=%d cost=$%.4f total=$%.4f",
            response.stop_reason, in_t, out_t, cr_t, cc_t, turn_cost, total_cost,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "tool_use":
            tool_use_blocks = [
                b for b in response.content if getattr(b, "type", None) == "tool_use"
            ]
            tool_results: list[dict[str, Any]] = []
            for tu in tool_use_blocks:
                total_tool_calls += 1
                t0 = time.monotonic()
                try:
                    result = dispatch(tu.name, dict(tu.input or {}), ctx)
                except Exception as e:
                    log.exception("Dispatch raised for %s", tu.name)
                    result = {"error": f"{type(e).__name__}: {e}"}
                duration_ms = int((time.monotonic() - t0) * 1000)

                err = None
                if isinstance(result, dict) and "error" in result:
                    err = str(result["error"])

                result_text = _safe_serialize(result)
                try:
                    episodic.log_action(
                        ctx.session_id,
                        tu.name,
                        _safe_serialize(tu.input),
                        result_text[:2000],
                        err,
                        duration_ms,
                    )
                except Exception:
                    log.exception("Failed to log action %s", tu.name)

                content_for_model = result_text
                if len(content_for_model) > TOOL_RESULT_TRUNC:
                    content_for_model = content_for_model[:TOOL_RESULT_TRUNC] + "...[truncated]"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": content_for_model,
                })

            messages.append({"role": "user", "content": tool_results})
            end_turn_without_finish = 0

            # Periodic budget recheck every 5 tool calls.
            if total_tool_calls and total_tool_calls % 5 == 0:
                try:
                    if episodic.cost_today() + total_cost >= daily_budget or \
                            episodic.cost_last_7_days() + total_cost >= weekly_budget:
                        log.warning("Budget recheck tripped mid-session; forcing finish")
                        ctx.finish_state = {
                            "current_focus": None,
                            "internal_state": None,
                            "decisions_made": None,
                            "journal_entry": None,
                            "next_invoke_minutes": None,
                            "end_reason": "forced_budget",
                        }
                except Exception:
                    log.exception("Budget recheck failed")

            # Honor pause request (clear after handling).
            if ctx.pause_requested_seconds:
                secs = int(ctx.pause_requested_seconds)
                log.info("Pause requested: %d seconds", secs)
                time.sleep(secs)
                ctx.pause_requested_seconds = None
                messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": f"[Paused for {secs}s. Continuing.]"}],
                })

            if ctx.finish_state is not None:
                break

        elif response.stop_reason == "end_turn":
            end_turn_without_finish += 1
            if end_turn_without_finish >= 2:
                log.warning("Agent ended turn twice without finish_session; forcing finish")
                ctx.finish_state = {
                    "current_focus": None,
                    "internal_state": None,
                    "decisions_made": None,
                    "journal_entry": None,
                    "next_invoke_minutes": None,
                    "end_reason": "end_turn_without_finish",
                }
                break
            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        "You stopped without calling finish_session. "
                        "If you're done, call finish_session now. "
                        "If you have more to do, continue."
                    ),
                }],
            })

        else:
            # max_tokens, stop_sequence, etc. Treat like end_turn-without-finish.
            log.warning("Unexpected stop_reason=%s; treating as end_turn", response.stop_reason)
            end_turn_without_finish += 1
            if end_turn_without_finish >= 2:
                ctx.finish_state = {
                    "current_focus": None,
                    "internal_state": None,
                    "decisions_made": None,
                    "journal_entry": None,
                    "next_invoke_minutes": None,
                    "end_reason": f"stop_reason_{response.stop_reason}",
                }
                break
            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        f"Your last turn ended with stop_reason={response.stop_reason}. "
                        "Please continue, or call finish_session if you're done."
                    ),
                }],
            })

    return {
        "tokens_in": total_in,
        "tokens_out": total_out,
        "cache_read": total_cache_read,
        "cache_creation": total_cache_creation,
        "cost_usd": total_cost,
        "tool_calls": total_tool_calls,
    }


def run_v1_session(instance: Instance) -> int:
    """The existing single-session flow, re-pathed to the given instance."""
    cfg = instance.config

    # Step 0 — self-clear our cron entry first.
    try:
        cron_control.remove_instance_entries(instance.id)
    except Exception:
        log.exception("Failed to remove existing cron entry; continuing")

    # Step 1 — lockfile (single-instance).
    if not lockfile.acquire(instance.lock_path):
        held = lockfile.read_pid(instance.lock_path)
        log.warning("Another orchestrator appears to be running (pid=%s); exiting", held)
        return 0
    atexit.register(lockfile.release, instance.lock_path)

    # Shared embedding cache — load the model from the shared HF cache rather
    # than re-downloading per instance.
    os.environ.setdefault("HF_HOME", str(SHARED_HF_CACHE))

    # Init stores + clients (instance-scoped paths).
    episodic = EpisodicStore(instance.episodes_db)
    semantic = SemanticStore(instance.vectors_dir)

    # Record this wake in the registry (best-effort).
    try:
        reg = load_registry()
        ent = registry_entry(reg, instance.id)
        if ent is not None:
            ent["last_wake"] = now_iso()
            save_registry(reg)
    except Exception:
        log.exception("Failed to update registry last_wake; continuing")

    session_id_box: dict[str, int | None] = {"id": None}
    _install_signal_handlers(
        instance=instance, episodic=episodic, session_id_box=session_id_box
    )

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
    # Config (config.json) drives runtime params; .env supplies fallbacks.
    model = instance.model
    max_tokens = int(cfg.get("max_tokens", _env("ANTHROPIC_MAX_TOKENS", "4096")))

    weekly_budget = float(cfg.get("weekly_cost_cap_usd", _env("WEEKLY_BUDGET_USD", "300")))
    daily_budget = float(cfg.get("daily_cost_cap_usd", _env("DAILY_BUDGET_USD", "50")))
    session_cost_cap = float(cfg.get("session_cost_cap_usd", _env("SESSION_COST_CAP_USD", "20.0")))
    max_tool_calls = int(cfg.get("max_tool_calls_per_session", _env("MAX_TOOL_CALLS_PER_SESSION", "500")))
    max_wall = float(cfg.get("max_session_wall_clock_seconds", _env("MAX_SESSION_WALL_CLOCK_SECONDS", "14400")))
    min_interval_minutes = int(cfg.get("min_interval_minutes", 30))

    # Step 2 — pre-flight budget check.
    cost_today = episodic.cost_today()
    cost_week = episodic.cost_last_7_days()
    if cost_today >= daily_budget:
        _budget_pause_and_notify(
            slack, episodic,
            instance_id=instance.id,
            reason=f"Daily cap reached: ${cost_today:.2f} / ${daily_budget:.2f}",
        )
        return 0
    if cost_week >= weekly_budget:
        _budget_pause_and_notify(
            slack, episodic,
            instance_id=instance.id,
            reason=f"Weekly cap reached: ${cost_week:.2f} / ${weekly_budget:.2f}",
        )
        return 0

    # Step 3 — periodic maintenance (cheap, no API).
    try:
        written = run_weekly_compression(episodic, semantic)
        if written:
            log.info("Wrote %d weekly summaries", len(written))
    except Exception:
        log.exception("Weekly compression failed; continuing")

    # Step 4 — session + context + tool-use loop.
    invocation_num = episodic.next_invocation_num()
    session_id = episodic.start_session(invocation_num, pid=os.getpid())
    session_id_box["id"] = session_id
    t_start = time.monotonic()
    log.info("Session %d started for invocation %d (pid=%d)", session_id, invocation_num, os.getpid())

    # Fetch any DMs Ben has sent since last we delivered. We persist the
    # newest seen ts only AFTER log_episode succeeds — so a crash mid-session
    # means we'll re-show the same DM next time rather than losing it.
    inbound_dms: list[dict[str, Any]] = []
    if slack is not None:
        last_seen_ts = episodic.get_meta("last_seen_ben_dm_ts", "0") or "0"
        inbound_dms = slack.fetch_dms_from_ben(oldest_ts=last_seen_ts)
        for m in inbound_dms:
            episodic.log_ben_contact(
                invocation_num=invocation_num,
                direction="in",
                channel="dm",
                body=m["text"],
            )
        if inbound_dms:
            log.info("Fetched %d inbound DM(s) from Ben", len(inbound_dms))

    inbound_texts = [m["text"] for m in inbound_dms]
    user_prompt, ctx_meta = assemble_context(
        episodic, semantic,
        workspace_dir=instance.workspace_dir,
        inbound_ben_messages=inbound_texts or None,
    )
    log.info("Assembled context: %s (inbound_dms=%d)", ctx_meta, len(inbound_dms))

    ctx = ToolContext(
        episodic=episodic,
        semantic=semantic,
        slack=slack,
        brave=brave,
        anthropic=anthropic_client,
        session_id=session_id,
        invocation_num=invocation_num,
        agent_root=AGENT_ROOT,
        workspace_dir=instance.workspace_dir,
    )

    try:
        stats = _run_tool_use_loop(
            anthropic_client=anthropic_client,
            model=model,
            max_tokens=max_tokens,
            user_prompt=user_prompt,
            ctx=ctx,
            episodic=episodic,
            daily_budget=daily_budget,
            weekly_budget=weekly_budget,
            max_tool_calls=max_tool_calls,
            max_wall=max_wall,
            cost_cap=session_cost_cap,
            t_start=t_start,
        )
    except Exception:
        log.exception("Tool-use loop crashed")
        stats = {
            "tokens_in": 0, "tokens_out": 0,
            "cache_read": 0, "cache_creation": 0,
            "cost_usd": 0.0, "tool_calls": 0,
        }
        if ctx.finish_state is None:
            ctx.finish_state = {
                "current_focus": None, "internal_state": None,
                "decisions_made": None, "journal_entry": None,
                "next_invoke_minutes": None, "end_reason": "loop_crash",
            }

    # Step 5 — finalize. Pull finish_state; clamp next_invoke_minutes.
    fs = ctx.finish_state or {
        "current_focus": None,
        "internal_state": None,
        "decisions_made": None,
        "journal_entry": None,
        "next_invoke_minutes": None,
        "end_reason": "no_finish_called",
    }
    nim_raw = fs.get("next_invoke_minutes")
    if isinstance(nim_raw, (int, float)):
        nim = int(nim_raw)
        if nim < min_interval_minutes:
            log.warning("next_invoke_minutes=%d clamped to %d", nim, min_interval_minutes)
            nim = min_interval_minutes
        next_invoke_minutes: int | None = nim
    else:
        next_invoke_minutes = None

    wall_clock = time.monotonic() - t_start
    session_actions = episodic.actions_for_session(session_id)
    actions_taken = _summarize_actions(session_actions)
    current_focus = fs.get("current_focus") or (
        f"see actions_taken ({len(session_actions)} tool calls)"
        if session_actions else None
    )

    raw_output_payload = _safe_serialize(fs)

    episode_id = episodic.log_episode(
        invocation_num=invocation_num,
        current_focus=current_focus,
        actions_taken=actions_taken,
        decisions_made=fs.get("decisions_made"),
        internal_state=fs.get("internal_state"),
        journal_entry=fs.get("journal_entry"),
        next_invoke_minutes=next_invoke_minutes,
        raw_output=raw_output_payload,
        tokens_in=stats["tokens_in"],
        tokens_out=stats["tokens_out"],
        cost_usd=stats["cost_usd"],
        parse_error=None,
        session_id=session_id,
        wall_clock_seconds=wall_clock,
    )

    if inbound_dms:
        newest_ts = max(m["ts"] for m in inbound_dms)
        episodic.set_meta("last_seen_ben_dm_ts", newest_ts)
        log.info("Advanced last_seen_ben_dm_ts to %s", newest_ts)
    episodic.add_cost(
        tokens_in=stats["tokens_in"],
        tokens_out=stats["tokens_out"],
        cost_usd=stats["cost_usd"],
    )
    episodic.update_session_stats(
        session_id,
        total_tool_calls=stats["tool_calls"],
        total_cost_usd=stats["cost_usd"],
    )
    episodic.end_session(
        session_id,
        status="finished",
        end_reason=fs.get("end_reason"),
    )

    # Embed into semantic store.
    try:
        emb_text = summarize_episode_for_embedding({
            "invocation_num": invocation_num,
            "timestamp": datetime.now(UTC).isoformat(),
            "current_focus": current_focus,
            "actions_taken": actions_taken,
            "decisions_made": fs.get("decisions_made"),
            "internal_state": fs.get("internal_state"),
            "journal_entry": fs.get("journal_entry"),
        })
        if emb_text.strip():
            semantic.add_episode(
                episode_id=episode_id,
                invocation_num=invocation_num,
                timestamp=datetime.now(UTC).isoformat(),
                text=emb_text,
            )
    except Exception:
        log.exception("Embedding failed; episode is logged but not searchable")

    # Mirror to observer channel (orchestrator-driven).
    if slack:
        observer_text = format_episode_for_observer(
            invocation_num=invocation_num,
            timestamp=datetime.now(UTC).isoformat(),
            current_focus=current_focus,
            actions=actions_taken,
            decisions=fs.get("decisions_made"),
            internal_state=fs.get("internal_state"),
            journal_entry=fs.get("journal_entry"),
            capability_request=None,
            next_invoke_minutes=next_invoke_minutes,
            tokens_in=stats["tokens_in"],
            tokens_out=stats["tokens_out"],
            cost_usd=stats["cost_usd"],
            parse_error=None,
        )
        if slack.post_to_observer_channel(observer_text):
            episodic.log_ben_contact(
                invocation_num=invocation_num,
                direction="out",
                channel="observer_channel",
                body=observer_text,
            )

    # Final journal_entry from finish_session — post as end-of-session summary.
    # Incremental journal entries during the session were already posted via the tool.
    if slack and fs.get("journal_entry"):
        je = fs["journal_entry"]
        if slack.post_to_agent_channel(je):
            episodic.log_ben_contact(
                invocation_num=invocation_num,
                direction="out",
                channel="agent_channel",
                body=je,
            )

    log.info(
        "Session %d done. tool_calls=%d wall=%.1fs tokens=%d/%d cache_r=%d cache_w=%d cost=$%.4f end=%s",
        session_id, stats["tool_calls"], wall_clock,
        stats["tokens_in"], stats["tokens_out"],
        stats["cache_read"], stats["cache_creation"],
        stats["cost_usd"], fs.get("end_reason"),
    )

    # Step 6 — post-call budget recheck.
    cost_today = episodic.cost_today()
    cost_week = episodic.cost_last_7_days()
    over_daily = cost_today >= daily_budget
    over_weekly = cost_week >= weekly_budget
    if over_daily or over_weekly:
        reason = []
        if over_daily:
            reason.append(f"daily ${cost_today:.2f}/${daily_budget:.2f}")
        if over_weekly:
            reason.append(f"weekly ${cost_week:.2f}/${weekly_budget:.2f}")
        _budget_pause_and_notify(
            slack, episodic, instance_id=instance.id, reason=" and ".join(reason)
        )
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
            semantic=semantic, agent_root=AGENT_ROOT,
        )
    except Exception:
        log.exception("Research panel failed; continuing to schedule next wake")

    # Step 7 — schedule next invocation.
    if next_invoke_minutes is None:
        log.info("Agent chose not to reschedule. No cron entry installed.")
        cron_control.clear_instance(instance.id)
    else:
        try:
            installed = cron_control.install_instance_one_shot(
                instance.id,
                minutes_from_now=next_invoke_minutes,
            )
            log.info("Cron scheduled: %s", installed)
        except Exception:
            log.exception("Failed to install cron entry")

    return 0


def run_v2_session(instance: Instance) -> int:
    """v2 'environmental' tick loop (forced wakefulness logged-not-enforced + decay)."""
    from v2_session import run_v2_session as _run_v2
    return _run_v2(instance)


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent orchestrator (one session).")
    parser.add_argument("--instance", required=True, help="instance id to run")
    args = parser.parse_args()

    instance = load_instance(args.instance)
    instance.ensure_dirs()
    _configure_instance_logging(instance)

    log.info("Resolved instance %s (name=%s version=%s model=%s)",
             instance.id, instance.name, instance.version, instance.model)

    import instance_control
    if instance_control.is_paused(instance.id):
        log.info("Instance %s is PAUSED (operator maintenance); removing any cron entry and exiting without running a session.", instance.id)
        try:
            cron_control.remove_instance_entries(instance.id)
        except Exception:
            log.exception("failed to remove cron entry while paused")
        return 0

    if instance.version == "v1":
        return run_v1_session(instance)
    if instance.version == "v2":
        return run_v2_session(instance)
    if instance.version == "v3":
        from v3_session import run_v3_session
        return run_v3_session(instance)
    if instance.version == "v4":
        from v4_session import run_v4_session
        return run_v4_session(instance)
    log.error("Instance %s has unknown version %r; aborting", instance.id, instance.version)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("Unhandled exception in orchestrator")
        sys.exit(1)
