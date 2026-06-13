"""Assemble the per-invocation context block fed to Claude.

Per spec:
  1. Last 3 episodes in full
  2. Top-k semantic matches from longer history (focus-driven query)
  3. Pending capability requests + status
  4. Current datetime, invocation number, days since start
  5. Slack messages from Ben (if any) — handled separately by orchestrator
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from instances_common import notes_path
from memory.episodic import EpisodicStore
from memory.semantic import SemanticStore

UTC = timezone.utc


def load_claude_md(workspace_dir: Path) -> str | None:
    p = notes_path(workspace_dir)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8").rstrip()
    except Exception:
        return None


def _fmt_episode(ep: dict[str, Any]) -> str:
    parts = [
        f"--- Episode #{ep.get('invocation_num')} ({ep.get('timestamp')}) ---"
    ]
    if ep.get("current_focus"):
        parts.append(f"focus: {ep['current_focus']}")
    actions = ep.get("actions_taken") or []
    if actions:
        parts.append("actions_taken:")
        for a in actions:
            parts.append(f"  - {a}")
    if ep.get("decisions_made"):
        parts.append(f"decisions_made: {ep['decisions_made']}")
    if ep.get("internal_state"):
        parts.append(f"internal_state: {ep['internal_state']}")
    if ep.get("journal_entry"):
        parts.append(f"journal_entry: {ep['journal_entry']}")
    if ep.get("next_invoke_minutes") is not None:
        parts.append(f"next_invoke_minutes: {ep['next_invoke_minutes']}")
    return "\n".join(parts)


def _derive_focus_query(recent: list[dict[str, Any]]) -> str | None:
    """Use the most recent episode's focus to seed semantic retrieval.

    Falls back to its journal entry text, then to actions, then None.
    """
    for ep in reversed(recent):
        if ep.get("current_focus"):
            return str(ep["current_focus"])
        if ep.get("journal_entry"):
            return str(ep["journal_entry"])[:500]
        if ep.get("actions_taken"):
            return " ".join(str(a) for a in ep["actions_taken"])[:500]
    return None


def assemble_context(
    episodic: EpisodicStore,
    semantic: SemanticStore,
    *,
    inbound_ben_messages: list[str] | None = None,
    semantic_k: int = 5,
    agent_root: Path | None = None,
    workspace_dir: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    """Returns (user_prompt_text, metadata).

    The system prompt is held separately. This is the assembled user message.
    """
    now = datetime.now(UTC)
    invocation_num = episodic.next_invocation_num()
    start = episodic.start_date()
    days_since_start = (now - start).days

    recent = episodic.recent_episodes(n=3)
    focus_query = _derive_focus_query(recent)

    semantic_hits: list[dict[str, Any]] = []
    if focus_query:
        # Avoid resurfacing the very recent episodes via semantic search
        recent_ids = {ep.get("id") for ep in recent}
        for hit in semantic.search(focus_query, k=semantic_k + len(recent)):
            if hit.get("episode_id") in recent_ids:
                continue
            semantic_hits.append(hit)
            if len(semantic_hits) >= semantic_k:
                break

    pending_caps = episodic.pending_capability_requests()

    blocks: list[str] = []

    claude_md_content: str | None = None
    ws = workspace_dir if workspace_dir is not None else (
        agent_root / "agent_workspace" if agent_root is not None else None
    )
    if ws is not None:
        claude_md_content = load_claude_md(ws)
    if claude_md_content:
        blocks.append(
            "=== Your AGENTS.md (notes you have written to yourself) ===\n"
            f"{claude_md_content}"
        )

    blocks.append(
        f"Current datetime (UTC): {now.isoformat()}\n"
        f"Invocation number: {invocation_num}\n"
        f"Days since start: {days_since_start}"
    )

    if recent:
        blocks.append(
            "=== Last episodes (most recent last) ===\n"
            + "\n\n".join(_fmt_episode(ep) for ep in recent)
        )
    else:
        blocks.append("=== Last episodes ===\n(none — this is your first invocation.)")

    if semantic_hits:
        sem_block = ["=== Semantic recall (related earlier episodes) ==="]
        for hit in semantic_hits:
            sem_block.append(
                f"[ep #{hit.get('invocation_num')} @ {hit.get('timestamp')}]"
                f"\n{hit.get('text','').strip()}"
            )
        blocks.append("\n\n".join(sem_block))

    if pending_caps:
        cap_lines = ["=== Pending capability requests ==="]
        for c in pending_caps:
            cap_lines.append(
                f"  - id={c['id']} '{c['capability']}' "
                f"(asked at {c['timestamp']}) — status: {c['status']}"
            )
        blocks.append("\n".join(cap_lines))

    if inbound_ben_messages:
        blocks.append(
            "=== Messages from Ben since last invocation ===\n"
            + "\n\n".join(f"Ben: {m}" for m in inbound_ben_messages)
        )

    blocks.append(
        "You are running as a long-lived session that started on cron wake-up. "
        "You have tools available — call them as needed in any sequence. "
        "When you're done thinking for now, call the finish_session tool with your "
        "final state (current_focus, internal_state, decisions_made, journal_entry, "
        "next_invoke_minutes). The process exits when finish_session is called. "
        "Minimum next_invoke_minutes is 30 (lower values will be clamped). "
        "If next_invoke_minutes is null, no further cron entry is installed and "
        "you will not be woken again unless Ben manually restarts you."
    )

    user_text = "\n\n".join(blocks)
    meta = {
        "invocation_num": invocation_num,
        "days_since_start": days_since_start,
        "focus_query": focus_query,
        "semantic_hits": len(semantic_hits),
        "recent_episodes": len(recent),
        "pending_caps": len(pending_caps),
        "claude_md_loaded": claude_md_content is not None,
    }
    return user_text, meta
