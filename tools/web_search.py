"""Brave Search API client (Web search endpoint)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


@dataclass
class SearchResult:
    title: str
    url: str
    description: str


class BraveSearch:
    def __init__(self, api_key: str, timeout: float = 15.0):
        self._api_key = api_key
        self._timeout = timeout

    def search(self, query: str, *, count: int = 5) -> list[SearchResult]:
        if not query.strip():
            return []
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }
        params = {"q": query, "count": min(max(count, 1), 20)}
        r = requests.get(_ENDPOINT, headers=headers, params=params, timeout=self._timeout)
        if r.status_code != 200:
            logger.error("Brave search %s -> HTTP %d: %s", query, r.status_code, r.text[:200])
            r.raise_for_status()
        data: dict[str, Any] = r.json()
        items = (data.get("web") or {}).get("results") or []
        return [
            SearchResult(
                title=item.get("title", "") or "",
                url=item.get("url", "") or "",
                description=item.get("description", "") or "",
            )
            for item in items
        ]


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv("/home/claudebot/agent/.env")
    s = BraveSearch(os.environ["BRAVE_API_KEY"])
    for r in s.search("anthropic claude api"):
        print(f"- {r.title}\n  {r.url}\n  {r.description[:120]}")
