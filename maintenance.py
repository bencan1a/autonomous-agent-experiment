"""Periodic maintenance — weekly compression of older episodes.

Spec: 'Older episodes (>7 days) compressed into weekly summaries, raw retained.'
Run is idempotent: it only writes a weekly summary for an ISO-week that
doesn't have one yet AND for which all episodes in that week are >7d old.
The summary is deterministic concatenation (no extra LLM cost) and is then
embedded into the semantic store so it surfaces in retrieval.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from memory.episodic import EpisodicStore
from memory.semantic import SemanticStore

logger = logging.getLogger(__name__)
UTC = timezone.utc

_MAX_SUMMARY_CHARS = 4000


def _iso_week_key(ts: str) -> tuple[int, int]:
    dt = datetime.fromisoformat(ts)
    iso = dt.isocalendar()
    return (iso.year, iso.week)


def _summarize_week(episodes: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for ep in episodes:
        bit = f"#{ep['invocation_num']} ({ep['timestamp']})"
        if ep.get("current_focus"):
            bit += f" focus={ep['current_focus']!r}"
        if ep.get("internal_state"):
            bit += f" state={ep['internal_state']!r}"
        if ep.get("journal_entry"):
            j = ep["journal_entry"]
            if len(j) > 400:
                j = j[:400] + "…"
            bit += f"\n  journal: {j}"
        if ep.get("decisions_made"):
            d = ep["decisions_made"]
            if len(d) > 200:
                d = d[:200] + "…"
            bit += f"\n  decisions: {d}"
        lines.append(bit)
    summary = "\n".join(lines)
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[:_MAX_SUMMARY_CHARS] + "\n…[truncated]"
    return summary


def run_weekly_compression(
    episodic: EpisodicStore, semantic: SemanticStore
) -> list[tuple[str, str]]:
    """Compress >7-day-old episodes into per-ISO-week summaries (idempotent)."""
    old = episodic.episodes_older_than(7)
    if not old:
        return []

    existing = {
        (s["week_start"], s["week_end"])
        for s in episodic.all_weekly_summaries()
    }

    by_week: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for ep in old:
        try:
            key = _iso_week_key(ep["timestamp"])
        except Exception:
            continue
        by_week.setdefault(key, []).append(ep)

    written: list[tuple[str, str]] = []
    for (year, week), eps in sorted(by_week.items()):
        # Compute week_start (Monday) and week_end (Sunday) ISO dates
        monday = datetime.fromisocalendar(year, week, 1).replace(tzinfo=UTC)
        sunday = monday + timedelta(days=6)
        ws, we = monday.date().isoformat(), sunday.date().isoformat()
        if (ws, we) in existing:
            continue
        # Only summarize if the entire week's worth of episodes is >7d old
        if sunday > datetime.now(UTC) - timedelta(days=7):
            continue
        summary = _summarize_week(eps)
        summary_id = episodic.add_weekly_summary(ws, we, summary)
        # Embed for retrieval
        try:
            semantic.add_episode(
                episode_id=-summary_id,  # sentinel: negative id = weekly summary
                invocation_num=eps[0]["invocation_num"],
                timestamp=ws,
                text=f"Weekly summary {ws}..{we}\n{summary}",
                kind="weekly_summary",
            )
        except Exception:
            logger.exception("Failed to embed weekly summary %s..%s", ws, we)
        written.append((ws, we))
        logger.info("Wrote weekly summary %s..%s (%d episodes)", ws, we, len(eps))
    return written
