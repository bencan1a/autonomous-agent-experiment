"""Seed a forked instance's Slack channel with the parent channel's history.

Slack has no channel-clone API, so we read the parent channel's messages
(oldest-first) and re-post them into the child channel AS THE BOT, each prefixed
with the original author + timestamp. This is for the human observer's benefit
(each branch's channel reads coherently) — the *agent's* continuity comes from
the copied episodes.db, not from channel scrollback.

Re-fetch safety: replayed posts are bot-authored, and ``SlackClient.fetch_dms_from_ben``
keeps only messages whose ``user == ben_user_id`` (dropping bot/subtype messages).
So a forked agent can never mistake a replayed line for a *new* message from Ben.

Fidelity is best-effort and lossy: threading is flattened, original timestamps
become text (not real message times), and reactions / files / edits are dropped.
Every function here is non-fatal — a replay failure must never abort a fork.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


def _resolve_author(client: WebClient, msg: dict, ben_user_id: str, cache: dict) -> str:
    """Best-effort human label for a message's author."""
    if msg.get("bot_id") and not msg.get("user"):
        return msg.get("username") or "bot"
    uid = msg.get("user")
    if not uid:
        return "system"
    if uid == ben_user_id:
        return "Ben"
    if uid in cache:
        return cache[uid]
    label = uid
    try:
        info = client.users_info(user=uid)
        prof = (info.data.get("user") or {}).get("profile") or {}
        label = prof.get("display_name") or prof.get("real_name") or uid
    except SlackApiError as e:
        logger.debug("users_info(%s) failed: %s", uid, e.response.get("error"))
    cache[uid] = label
    return label


def _read_all_history(client: WebClient, channel_id: str, limit: int) -> list[dict]:
    """Page conversations.history oldest-first, up to ``limit`` messages."""
    out: list[dict] = []
    cursor: str | None = None
    while len(out) < limit:
        resp = client.conversations_history(
            channel=channel_id, limit=min(200, limit - len(out)), cursor=cursor,
        )
        out.extend(resp.data.get("messages") or [])
        cursor = (resp.data.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    # Slack returns newest-first; we want oldest-first for natural reading order.
    out.sort(key=lambda m: float(m.get("ts", "0")))
    return out[:limit]


def replay_channel_history(
    *,
    bot_token: str,
    src_channel_id: str,
    dst_channel_id: str,
    ben_user_id: str,
    limit: int = 1000,
    header: str | None = None,
) -> dict[str, int]:
    """Replay ``src_channel_id``'s history into ``dst_channel_id`` as the bot.

    Returns ``{"posted", "skipped", "errors"}``. Never raises — all failures are
    logged and counted so a caller can treat replay as best-effort.
    """
    stats = {"posted": 0, "skipped": 0, "errors": 0}
    if not src_channel_id or not dst_channel_id:
        return stats
    client = WebClient(token=bot_token)
    cache: dict[str, str] = {}
    try:
        messages = _read_all_history(client, src_channel_id, limit)
    except SlackApiError as e:
        logger.warning("replay read failed for %s: %s", src_channel_id, e.response.get("error"))
        stats["errors"] += 1
        return stats

    if header:
        try:
            client.chat_postMessage(channel=dst_channel_id, text=header)
        except SlackApiError as e:
            logger.debug("replay header post failed: %s", e.response.get("error"))

    for m in messages:
        if m.get("subtype") and m.get("subtype") != "bot_message":
            stats["skipped"] += 1
            continue
        text = m.get("text")
        if not text:
            stats["skipped"] += 1
            continue
        author = _resolve_author(client, m, ben_user_id, cache)
        ts = m.get("ts", "")
        try:
            human_ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
        except (TypeError, ValueError):
            human_ts = ts
        body = f"*[replay] {author} · {human_ts}:*\n{text}"
        try:
            client.chat_postMessage(channel=dst_channel_id, text=body)
            stats["posted"] += 1
        except SlackApiError as e:
            err = e.response.get("error")
            if err == "ratelimited":
                retry = int(e.response.headers.get("Retry-After", "2"))
                time.sleep(retry)
                try:
                    client.chat_postMessage(channel=dst_channel_id, text=body)
                    stats["posted"] += 1
                    continue
                except SlackApiError:
                    pass
            logger.debug("replay post failed: %s", err)
            stats["errors"] += 1
    logger.info(
        "replayed %s -> %s: %s", src_channel_id, dst_channel_id, stats
    )
    return stats
