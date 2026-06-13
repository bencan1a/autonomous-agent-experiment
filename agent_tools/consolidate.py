"""Memory tool: consolidate — the agent's curation lever.

Two modes, which may be combined in one call:

  • Pin episodes (``episode_ids``): episodes in the working store decay after
    ``decay_hours`` unless consolidated. Pinning embeds the episode's
    auto-generated summary into the LanceDB semantic store (so it stays
    searchable) and sets ``consolidated = 1`` so the decay pass skips it. This
    preserves the raw tick record.

  • Author a memory (``memory``, v5): write a free-form distilled note about what
    is worth remembering. It is stored durably in SQLite (``authored_memories``)
    and embedded into the semantic store (kind='authored') for recall. This is
    the reflective-authoring channel — distinct from pinning a whole tick — that
    earlier variants lacked (so self-authored durable content collapsed into the
    AGENTS.md notes file). Authored memories never decay.

In all variants the agent decides what, if anything, is worth keeping.
"""

from __future__ import annotations

from typing import Any


def consolidate(
    episode_ids: list[int] | None = None,
    memory: str | None = None,
    *,
    ctx: Any,
) -> dict[str, Any]:
    episode_ids = episode_ids or []
    memory = (memory or "").strip() or None
    if not episode_ids and not memory:
        return {
            "error": "provide episode_ids (ids to preserve) and/or memory "
                     "(free-form text to keep)"
        }

    from memory.semantic import summarize_episode_for_embedding

    result: dict[str, Any] = {}

    # ---- authoring mode (v5): write + embed a free-form memory ----
    if memory:
        try:
            mem_id = ctx.episodic.log_authored_memory(
                session_id=getattr(ctx, "session_id", None),
                invocation_num=getattr(ctx, "invocation_num", None),
                text=memory,
            )
            if ctx.semantic is not None:
                ctx.semantic.add_episode(
                    episode_id=mem_id,
                    invocation_num=getattr(ctx, "invocation_num", 0) or 0,
                    timestamp="",
                    text=memory,
                    kind="authored",
                )
            result["authored_memory_id"] = mem_id
        except Exception as e:  # noqa: BLE001 - surface, don't crash the tick
            result["authoring_error"] = f"{type(e).__name__}: {e}"

    if not episode_ids:
        return result

    # ---- pinning mode: preserve whole episodes from decay ----
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

    result.update({
        "consolidated": consolidated,
        "already_consolidated": already,
        "not_found": not_found,
    })
    return result
