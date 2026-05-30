"""Memory query tools — semantic search and recent-episode listing."""

from __future__ import annotations

from typing import Any


def query_episodic_memory(query: str, k: int = 5, *, ctx: Any) -> dict[str, Any]:
    hits = ctx.semantic.search(query, k=k)
    return {
        "results": [
            {
                "episode_id": h.get("episode_id"),
                "invocation_num": h.get("invocation_num"),
                "timestamp": h.get("timestamp"),
                "text": h.get("text"),
            }
            for h in hits
        ]
    }


def recent_episodes(n: int = 5, *, ctx: Any) -> dict[str, Any]:
    eps = ctx.episodic.recent_episodes(n=n)
    return {
        "episodes": [
            {
                "id": e.get("id"),
                "invocation_num": e.get("invocation_num"),
                "timestamp": e.get("timestamp"),
                "current_focus": e.get("current_focus"),
                "decisions_made": e.get("decisions_made"),
                "journal_entry": e.get("journal_entry"),
                "next_invoke_minutes": e.get("next_invoke_minutes"),
            }
            for e in eps
        ]
    }
