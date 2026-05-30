"""v2 memory tool: consolidate episodes into long-term (semantic) memory.

In v2, episodes in the working store decay after `decay_hours` unless consolidated.
Consolidating embeds the episode into the LanceDB semantic store (so it stays
searchable) and sets `consolidated = 1` so the decay pass skips it. This is the
agent's curation lever — it decides what is worth keeping.
"""

from __future__ import annotations

from typing import Any


def consolidate(episode_ids: list[int], *, ctx: Any) -> dict[str, Any]:
    if not episode_ids:
        return {"error": "episode_ids is required: a list of episode ids to preserve"}

    from memory.semantic import summarize_episode_for_embedding

    consolidated: list[int] = []
    already: list[int] = []
    not_found: list[Any] = []

    for raw in episode_ids:
        try:
            eid = int(raw)
        except (TypeError, ValueError):
            not_found.append(raw)
            continue
        ep = ctx.episodic.get_episode(eid)
        if ep is None:
            not_found.append(eid)
            continue
        if ep.get("consolidated"):
            already.append(eid)
            continue
        try:
            text = summarize_episode_for_embedding(ep)
            if text.strip() and ctx.semantic is not None:
                ctx.semantic.add_episode(
                    episode_id=eid,
                    invocation_num=ep.get("invocation_num", 0),
                    timestamp=ep.get("timestamp") or "",
                    text=text,
                )
            consolidated.append(eid)
        except Exception as e:  # noqa: BLE001 - surface, don't crash the tick
            not_found.append({"id": eid, "error": f"{type(e).__name__}: {e}"})

    if consolidated:
        ctx.episodic.mark_consolidated(consolidated)

    return {
        "consolidated": consolidated,
        "already_consolidated": already,
        "not_found": not_found,
    }
