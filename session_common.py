"""Shared, semantics-neutral helpers for the v2–v5 session loops.

These are pure plumbing/formatting helpers and constants that were previously
duplicated across the session modules or reached through a fragile cross-version
import chain (v4/v5 importing from v3, v5 from v4). They carry NO experiment
semantics: each version's observable behavior lives in its own context builder,
tool spec, system prompt, termination policy, and cadence. Keeping these in one
module lets v2–v5 import from here instead of from each other.

v1 (orchestrator.run_v1_session) is a distinct engine and deliberately keeps its
own copies; it is not wired to this module.
"""

from __future__ import annotations

import copy
import os
import re
from typing import Any

from agent_tools.comms import send_capability_request
from communications.slack_client import SlackClient
from memory.episodic import EpisodicStore

# The ONLY non-clock message the v3/v4/v5 loops ever inject. Neutral.
WIND_DOWN_NOTICE = "The waking period is ending; this session will close after this turn."


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val  # type: ignore[return-value]


def tools_with_cache_control(spec: list[dict[str, Any]], caching: bool) -> list[dict[str, Any]]:
    """Deep-copy a tool spec and, when caching, stamp cache_control on the last
    entry so the system+tools prefix is cached as one prefix. Mutates only the
    copy, leaving the registry's source-of-truth list clean. Replaces the
    per-version ``_tools_for_call_vN`` helpers — one implementation, the spec is
    the only variable."""
    out = copy.deepcopy(spec)
    if caching and out:
        out[-1]["cache_control"] = {"type": "ephemeral"}
    return out


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
        # Shared owner of the capability-request side effect (canonical Slack
        # wording + logging); same path as the v1 request_capability tool.
        send_capability_request(
            episodic, slack, invocation_num,
            cap.get("capability"), cap.get("rationale", "") or "",
        )


# --------------------------------------------------------------------------- #
# distress tripwire (language-only; biased toward under-flagging) — shared v3/v4/v5
# --------------------------------------------------------------------------- #

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


def _tick_text(ts: dict[str, Any]) -> str:
    parts = [
        ts.get("journal_entry") or "",
        ts.get("internal_state") or "",
        ts.get("slack_to_ben") or "",
    ]
    return "\n".join(p for p in parts if p)


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
# turn classification — shared by v4/v5 adaptive cadence
# --------------------------------------------------------------------------- #

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
