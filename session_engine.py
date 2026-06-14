"""Shared session engine for the v2–v5 experiment loops.

The four ``run_vN_session`` drivers were ~300–400 lines each that wrapped the
already-shared per-tick engine (``v2_session.run_one_tick``) in an identical
orchestration scaffold: cron self-clear, lockfile, store/client init, the
OpenRouter branch, the config/budget param block, registry ``last_wake``, the
budget preflight, decay, session start, the per-episode log/side-effect/observer
block, the finalize stats, the fatal-pause path, the post-session budget recheck,
the research panel, and the cron reschedule. All of that is *mechanism* — it is
identical across v2–v5 and is NOT an experimental variable — so it lives here.

What stays in each ``run_vN_session`` is the *science*: the loop shape itself
(v2 agent-controlled end + fixed tick; v3 wind-down + ``would_end_now`` logging +
fixed tick; v4/v5 system-owned wind-down + adaptive cadence + neutral-clock +
inbound polling), the system prompt / tool spec / context builder, the
version-specific session record (``log_v{2,3,4}_session``), and the reschedule
policy. The engine never decides any of those; it only provides the plumbing the
loops call. This keeps each experiment's observable behavior explicit and
isolated while removing the duplicated scaffold.

v1 (``orchestrator.run_v1_session``) is a distinct ``finish_state`` engine and is
deliberately NOT built on this module.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

import cron_control
import lockfile
from agent_tools.registry import ToolContext
from communications.slack_client import SlackClient, format_episode_for_observer
from instances_common import Instance, SHARED_HF_CACHE, now_iso, registry_txn
from memory.episodic import EpisodicStore
from memory.semantic import SemanticStore
from openrouter_client import make_session_client, is_openrouter_model
from session_common import _env, _execute_side_effects
from tools.web_search import BraveSearch

logger = logging.getLogger("orchestrator.engine")

DEFAULT_FALLBACK_MINUTES = 60
DEFAULT_COMPACTION_TOKENS = 120_000


# --------------------------------------------------------------------------- #
# decay (shared; episodes past decay_hours that were not consolidated are deleted)
# --------------------------------------------------------------------------- #

def run_decay(
    episodic: EpisodicStore, decay_hours: float, semantic: SemanticStore | None = None,
) -> list[dict[str, Any]]:
    """Delete un-consolidated episodes past the decay horizon. Returns what was
    deleted (brief dicts) so the agent can be told, factually, what is now gone.

    When ``semantic`` is given, the same un-consolidated episodes' vectors are
    deleted from the semantic store too, so decayed memory doesn't linger in
    retrieval. Only the exact ids decay removes are targeted — consolidated /
    authored memory is never touched. A vector-store hiccup never breaks decay."""
    stale = episodic.unconsolidated_older_than_hours(decay_hours)
    if not stale:
        return []
    stale_ids = [e["id"] for e in stale]
    brief = [
        {
            "invocation_num": e.get("invocation_num"),
            "timestamp": e.get("timestamp"),
            "current_focus": e.get("current_focus"),
        }
        for e in stale
    ]
    episodic.delete_episodes(stale_ids)
    if semantic is not None:
        try:
            semantic.delete_by_episode_ids(stale_ids)
        except Exception:
            logger.exception("decay: failed to delete vectors for decayed episodes")
    logger.info("decay: deleted %d un-consolidated episode(s) past %sh", len(stale), decay_hours)
    return brief


# --------------------------------------------------------------------------- #
# pause / fatal / signal helpers (shared)
# --------------------------------------------------------------------------- #

def _budget_pause_and_notify(
    slack: SlackClient | None, episodic: EpisodicStore, *, instance_id: str, reason: str,
) -> None:
    logger.warning("BUDGET PAUSE: %s", reason)
    # Unified pause: sets registry status='paused' AND clears the schedule, so
    # `list`/dashboard/orchestrator all agree (clearing cron alone left the
    # registry showing 'active').
    try:
        import instance_control
        instance_control.pause(instance_id, reason=f"budget: {reason}")
    except Exception:
        logger.exception("failed to pause instance on budget breach")
        cron_control.clear_instance(instance_id)
    msg = (
        ":octagonal_sign: *Agent paused — budget exceeded.*\n"
        f"{reason}\nCron entry cleared. Re-enable manually after review."
    )
    if slack:
        slack.post_to_observer_channel(msg)
        if slack.dm_ben(msg):
            episodic.log_ben_contact(invocation_num=None, direction="out", channel="dm", body=msg)


def _fatal_pause_and_notify(
    slack: SlackClient | None, episodic: EpisodicStore, *, instance: Instance, reason: str,
) -> None:
    """Fail safe on an unexpected fatal session error (e.g. a model/API error such
    as a bad model slug that 404s). Pause the instance + clear its schedule so it
    can't crash-loop or sit as a zombie 'running' row, and notify the operator.
    Never raises (it runs from an except block)."""
    logger.error("FATAL SESSION ERROR (%s): %s", instance.id, reason)
    try:
        import instance_control
        # Unified pause: registry status='paused' + schedule cleared together.
        instance_control.pause(instance.id, reason=f"fatal session error: {reason}")
    except Exception:
        logger.exception("failed to pause instance on fatal error")
        try:
            cron_control.clear_instance(instance.id)
        except Exception:
            logger.exception("failed to clear cron on fatal error")
    msg = (
        ":rotating_light: *Agent paused — session error.*\n"
        f"{reason}\n"
        "The session was ended and no further wake is scheduled. Re-enable after review."
    )
    if slack:
        try:
            slack.post_to_observer_channel(msg)
            if slack.dm_ben(msg):
                episodic.log_ben_contact(invocation_num=None, direction="out", channel="dm", body=msg)
        except Exception:
            logger.exception("failed to notify operator on fatal error")


def _install_signal_handlers(*, instance: Instance, episodic: EpisodicStore, box: dict[str, Any]) -> None:
    def handler(signum: int, _frame: Any) -> None:
        logger.warning("Received signal %d; cleaning up", signum)
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
# session runtime (the shared setup output)
# --------------------------------------------------------------------------- #

@dataclass
class SessionRuntime:
    instance: Instance
    cfg: dict[str, Any]
    episodic: EpisodicStore
    semantic: SemanticStore
    slack: SlackClient | None
    brave: BraveSearch | None
    anthropic_client: Any
    client: Any                  # anthropic_client, or OpenRouterClient for OR models
    ctx: ToolContext
    model: str
    max_tokens: int
    caching: bool
    compaction_on: bool
    compaction_threshold: int
    decay_hours: float
    daily_budget: float
    weekly_budget: float
    session_cost_cap: float
    max_wall: float
    decayed: list[dict[str, Any]]
    invocation_num: int
    session_id: int
    started_at: str
    t_start: float
    box: dict[str, Any]
    inbound_dms: list[dict[str, Any]] = field(default_factory=list)
    last_seen: str = "0"


def setup_session(instance: Instance) -> SessionRuntime | None:
    """Run the identical session preamble (Steps 0–4) shared by v2–v5 and return a
    SessionRuntime. Returns None when the session must not proceed — another
    orchestrator holds the lock, or a budget cap is already tripped (in which case
    the instance has been paused + notified). The caller returns 0 on None.

    Does NOT build the system prompt / tools / opening context / message list:
    those are version-specific and stay in each run_vN_session.
    """
    cfg = instance.config

    # Step 0 — self-clear our cron entry.
    try:
        cron_control.remove_instance_entries(instance.id)
    except Exception:
        logger.exception("Failed to remove cron entry; continuing")

    # Step 1 — lockfile.
    if not lockfile.acquire(instance.lock_path):
        held = lockfile.read_pid(instance.lock_path)
        logger.warning("Another orchestrator running for %s (pid=%s); exiting", instance.id, held)
        return None
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
        logger.exception("Slack init failed; continuing without Slack")
        slack = None
    try:
        brave: BraveSearch | None = BraveSearch(_env("BRAVE_API_KEY", required=True))
    except Exception:
        logger.exception("Brave init failed; web_search will error")
        brave = None
    # params (config over env defaults)
    model = instance.model
    max_tokens = int(cfg.get("max_tokens", 4096))

    # Loop client + caching flag (Anthropic SDK for Claude models, OpenRouter
    # adapter for slug models; caching is Anthropic-specific). The raw Anthropic
    # SDK (anthropic_client) is ALSO needed for the research panel / ToolContext:
    # for a Claude model the loop client already IS the SDK, so reuse it; for an
    # OpenRouter model build the SDK separately. Pass our (test-patchable)
    # ``anthropic`` module into the factory so monkeypatched tests flow through.
    client, client_caching = make_session_client(model, anthropic_mod=anthropic)
    caching = bool(cfg.get("prompt_caching", True)) and client_caching
    if is_openrouter_model(model):
        anthropic_client: Any = anthropic.Anthropic(api_key=_env("ANTHROPIC_API_KEY", required=True))
    else:
        anthropic_client = client  # already the Anthropic SDK
    compaction_on = bool(cfg.get("in_session_compaction", True))
    compaction_threshold = int(cfg.get("compaction_token_threshold", DEFAULT_COMPACTION_TOKENS))
    decay_hours = float(cfg.get("decay_hours", 72))
    daily_budget = float(cfg.get("daily_cost_cap_usd", _env("DAILY_BUDGET_USD", "50")))
    weekly_budget = float(cfg.get("weekly_cost_cap_usd", _env("WEEKLY_BUDGET_USD", "300")))
    session_cost_cap = float(cfg.get("session_cost_cap_usd", 20))
    max_wall = float(cfg.get("max_session_wall_clock_seconds", 14400))

    # registry last_wake
    try:
        with registry_txn() as reg:
            ent = reg.get("instances", {}).get(instance.id)
            if ent is not None:
                ent["last_wake"] = now_iso()
    except Exception:
        logger.exception("Failed to update registry last_wake; continuing")

    # Step 2 — budget preflight.
    if episodic.cost_today() >= daily_budget:
        _budget_pause_and_notify(slack, episodic, instance_id=instance.id,
                                 reason=f"Daily cap reached: ${episodic.cost_today():.2f}/${daily_budget:.2f}")
        return None
    if episodic.cost_last_7_days() >= weekly_budget:
        _budget_pause_and_notify(slack, episodic, instance_id=instance.id,
                                 reason=f"Weekly cap reached: ${episodic.cost_last_7_days():.2f}/${weekly_budget:.2f}")
        return None

    # Step 3 — decay.
    decayed = run_decay(episodic, decay_hours, semantic)

    # Step 4 — session start.
    invocation_num = episodic.next_invocation_num()
    session_id = episodic.start_session(invocation_num, pid=os.getpid())
    box["session_id"] = session_id
    started_at = now_iso()
    t_start = time.monotonic()
    logger.info("%s session %d started (invocation %d, pid %d)",
             instance.version, session_id, invocation_num, os.getpid())

    # inbound chat from Ben (also used to seed the reload context)
    last_seen = episodic.get_meta("last_seen_ben_dm_ts", "0") or "0"
    inbound_dms: list[dict[str, Any]] = []
    if slack is not None:
        inbound_dms = slack.fetch_dms_from_ben(oldest_ts=last_seen)
        for m in inbound_dms:
            episodic.log_ben_contact(invocation_num=invocation_num, direction="in", channel="dm", body=m["text"])
            last_seen = m["ts"]

    ctx = ToolContext(
        episodic=episodic, semantic=semantic, slack=slack, brave=brave,
        anthropic=anthropic_client, session_id=session_id, invocation_num=invocation_num,
        agent_root=instance.root.parent.parent, workspace_dir=instance.workspace_dir,
        model=model, client=client,
    )

    return SessionRuntime(
        instance=instance, cfg=cfg, episodic=episodic, semantic=semantic, slack=slack,
        brave=brave, anthropic_client=anthropic_client, client=client, ctx=ctx, model=model,
        max_tokens=max_tokens, caching=caching, compaction_on=compaction_on,
        compaction_threshold=compaction_threshold, decay_hours=decay_hours,
        daily_budget=daily_budget, weekly_budget=weekly_budget,
        session_cost_cap=session_cost_cap, max_wall=max_wall, decayed=decayed,
        invocation_num=invocation_num, session_id=session_id, started_at=started_at,
        t_start=t_start, box=box, inbound_dms=inbound_dms, last_seen=last_seen,
    )


# --------------------------------------------------------------------------- #
# in-loop helpers (shared)
# --------------------------------------------------------------------------- #

def cost_caps_tripped(rt: SessionRuntime, session_cost: float) -> str | None:
    """Check the three cost caps. On the first that's hit, pause + notify and
    return the end_reason; otherwise return None. (Wall-clock / max-ticks limits
    stay in each loop since they reference the loop's own counter.)"""
    if session_cost >= rt.session_cost_cap:
        _budget_pause_and_notify(rt.slack, rt.episodic, instance_id=rt.instance.id,
                                 reason=f"Session cap: ${session_cost:.2f}/${rt.session_cost_cap:.2f}")
        return "session_cost_cap"
    if rt.episodic.cost_today() >= rt.daily_budget:
        _budget_pause_and_notify(rt.slack, rt.episodic, instance_id=rt.instance.id,
                                 reason=f"Daily cap: ${rt.episodic.cost_today():.2f}/${rt.daily_budget:.2f}")
        return "daily_cap"
    if rt.episodic.cost_last_7_days() >= rt.weekly_budget:
        _budget_pause_and_notify(rt.slack, rt.episodic, instance_id=rt.instance.id,
                                 reason=f"Weekly cap: ${rt.episodic.cost_last_7_days():.2f}/${rt.weekly_budget:.2f}")
        return "weekly_cap"
    return None


def record_episode(
    rt: SessionRuntime, ts: dict[str, Any], tick: Any, *, elapsed: float,
    next_invoke_minutes: int | None = None, would_end_now: int = 0,
) -> None:
    """The identical per-tick block: persist the episode, run its Slack side
    effects, and mirror it to the observer channel. ``next_invoke_minutes`` and
    ``would_end_now`` are the only per-version knobs (v2 records the agent's
    chosen next wake on end; v3 records would_end_now; v4/v5 record neither)."""
    rt.episodic.log_episode(
        invocation_num=rt.invocation_num,
        current_focus=ts.get("tick_focus"),
        actions_taken=tick.actions,
        decisions_made=None,
        internal_state=ts.get("internal_state"),
        journal_entry=ts.get("journal_entry"),
        next_invoke_minutes=next_invoke_minutes,
        raw_output=json.dumps(ts, default=str),
        tokens_in=tick.tokens_in, tokens_out=tick.tokens_out, cost_usd=tick.cost_usd,
        session_id=rt.session_id, wall_clock_seconds=elapsed,
        would_end_now=would_end_now,
    )

    _execute_side_effects(ts, rt.slack, rt.episodic, rt.invocation_num)

    if rt.slack:
        observer = format_episode_for_observer(
            invocation_num=rt.invocation_num, timestamp=now_iso(),
            current_focus=ts.get("tick_focus"), actions=tick.actions,
            decisions=None, internal_state=ts.get("internal_state"),
            journal_entry=ts.get("journal_entry"),
            capability_request=ts.get("capability_request"),
            next_invoke_minutes=next_invoke_minutes,
            tokens_in=tick.tokens_in, tokens_out=tick.tokens_out,
            cost_usd=tick.cost_usd, parse_error=None,
        )
        if rt.slack.post_to_observer_channel(observer):
            rt.episodic.log_ben_contact(invocation_num=rt.invocation_num, direction="out",
                                        channel="observer_channel", body=observer)


# --------------------------------------------------------------------------- #
# finalize helpers (shared)
# --------------------------------------------------------------------------- #

def finalize_stats(
    rt: SessionRuntime, *, end_reason: str, fatal_error: str | None,
    num_ticks: int, session_cost: float, total_tool_calls: int,
) -> None:
    """Update aggregate session stats + mark the session ended. The
    version-specific ``log_v{2,3,4}_session`` record is written by the caller
    immediately after (it carries per-version fields)."""
    try:
        rt.episodic.update_session_stats(rt.session_id, total_tool_calls=total_tool_calls,
                                         total_cost_usd=session_cost)
        rt.episodic.end_session(rt.session_id,
                                status="killed" if fatal_error else "finished",
                                end_reason=end_reason)
    except Exception:
        logger.exception("Failed to finalize session stats")


def fatal_pause(rt: SessionRuntime, fatal_error: str) -> None:
    """Pause + notify on a fatal session error (caller then returns 0)."""
    _fatal_pause_and_notify(rt.slack, rt.episodic, instance=rt.instance,
                            reason=f"session error: {fatal_error}")


def post_session_budget_ok(rt: SessionRuntime) -> bool:
    """Post-session budget recheck. Pauses + notifies and returns False if a cap is
    now hit; otherwise True (safe to run the panel + schedule the next wake)."""
    if rt.episodic.cost_today() >= rt.daily_budget or rt.episodic.cost_last_7_days() >= rt.weekly_budget:
        _budget_pause_and_notify(rt.slack, rt.episodic, instance_id=rt.instance.id,
                                 reason="post-session budget cap reached")
        return False
    return True


def run_post_session_panel(rt: SessionRuntime) -> None:
    """Run the post-session research panel inline. Never blocks/raises into the
    next-wake scheduling."""
    try:
        from research.panel import run_research_panel
        from research.store import ResearchStore

        run_research_panel(
            instance=rt.instance, episodic=rt.episodic,
            research_store=ResearchStore(rt.instance.episodes_db),
            anthropic_client=rt.anthropic_client,
            session_id=rt.session_id, invocation_num=rt.invocation_num,
            semantic=rt.semantic, agent_root=rt.instance.root.parent.parent,
        )
    except Exception:
        logger.exception("Research panel failed; continuing to schedule next wake")


def schedule_next_wake(
    rt: SessionRuntime, minutes_from_now: int | None,
    *, min_minutes: int = cron_control.MIN_INTERVAL_MINUTES,
) -> None:
    """Install the one-shot cron for the next wake. ``None`` means the agent chose
    not to reschedule (v2 only) — clear the entry instead.

    ``min_minutes`` is the wake-interval floor forwarded to the single clamp
    authority (``cron_control.install_instance_one_shot``). v2 passes its
    configured ``min_interval_minutes``; v3/v4/v5 use the default
    ``MIN_INTERVAL_MINUTES``.

    If the operator paused the instance DURING the session (its registry status is
    now 'paused'), honor that: skip the reschedule and clear any cron, so the
    end-of-session reschedule can't resurrect a 'paused but counting down' orphan.
    We read the registry entry directly and skip only on an explicit 'paused'
    status — an absent entry (only ever the case in tests) reschedules as before.
    """
    from instances_common import load_registry, registry_entry
    ent = registry_entry(load_registry(), rt.instance.id)
    if ent is not None and ent.get("status") == "paused":
        logger.info("Instance paused during the session; skipping reschedule, clearing cron.")
        try:
            cron_control.clear_instance(rt.instance.id)
        except Exception:
            logger.exception("Failed to clear cron for paused instance")
        return
    if minutes_from_now is None:
        logger.info("No next wake scheduled; cron entry cleared.")
        cron_control.clear_instance(rt.instance.id)
        return
    try:
        cron_control.install_instance_one_shot(
            rt.instance.id, minutes_from_now=minutes_from_now, min_minutes=min_minutes,
        )
        logger.info("Next wake scheduled in %d min", minutes_from_now)
    except Exception:
        logger.exception("Failed to install cron entry")
