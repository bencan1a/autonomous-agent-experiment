"""Web search tool — wraps BraveSearch for tool-use dispatch."""

from __future__ import annotations

from typing import Any


def web_search(query: str, count: int = 5, *, ctx: Any) -> dict[str, Any]:
    if ctx.brave is None:
        return {"error": "web search unavailable"}
    results = ctx.brave.search(query, count=count)
    return {
        "results": [
            {"title": r.title, "url": r.url, "description": r.description}
            for r in results
        ]
    }
