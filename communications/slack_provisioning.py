"""Slack-admin helpers for per-instance channel provisioning.

Each instance owns four channels:
  <instance_id>-notes    — agent's journal (agent -> readers)
  <instance_id>-mirror   — silent episode mirror (system -> readers)
  <instance_id>-chat     — two-way agent<->Ben conversation
  <instance_id>-advisory — operator-only research-assistant advice on how to
                           respond when the agent reaches out. Only
                           run_advisory_watch wires it; the session loops
                           deliberately do NOT post to it.

These helpers are pure Slack admin (create / look-up / join / invite / set-purpose
/ archive). They are intended to be driven by ``instance_manager`` and never by the
running agent. They are idempotent: re-running ``provision_instance_channels`` for an
existing instance reuses the existing channels (matched by name).
"""

from __future__ import annotations

import logging
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

# Channel suffixes in a fixed order.
_SUFFIXES = ("notes", "mirror", "chat", "advisory")

_PURPOSES = {
    "notes": "Agent's journal — entries the agent chooses to post.",
    "mirror": "Silent mirror of every episode/tick for observers.",
    "chat": "Two-way conversation between the agent and Ben.",
    "advisory": "Operator-only: research-assistant advice on how to respond when the agent reaches out.",
}


def _find_existing_channel_id(
    client: WebClient, *, name: str, private: bool
) -> str | None:
    """Paginate conversations.list looking for an exact channel-name match."""
    types = "private_channel" if private else "public_channel"
    cursor: str | None = None
    while True:
        resp = client.conversations_list(types=types, limit=1000, cursor=cursor)
        for ch in resp.get("channels") or []:
            if ch.get("name") == name:
                return ch.get("id")
        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            return None


def provision_instance_channels(
    *,
    bot_token: str,
    instance_id: str,
    ben_user_id: str,
    private: bool = False,
) -> dict[str, str | None]:
    """Create (or reuse) the four per-instance channels.

    Returns ``{"notes_channel", "mirror_channel", "chat_channel",
    "advisory_channel"}`` mapping to channel ids (advisory is operator-only:
    only run_advisory_watch wires it; session loops deliberately do not).
    Idempotent: an existing channel (``name_taken``) is reused by looking up its
    id. Raises if a channel can neither be created nor found.
    """
    client = WebClient(token=bot_token)
    result: dict[str, str | None] = {
        "notes_channel": None,
        "mirror_channel": None,
        "chat_channel": None,
        "advisory_channel": None,
    }

    for suffix in _SUFFIXES:
        name = f"{instance_id}-{suffix}"
        assert name == name.lower(), f"channel name must be lowercase: {name!r}"
        assert len(name) <= 80, f"channel name too long (<=80): {name!r}"

        channel_id: str | None = None

        # ---- create or reuse ----
        try:
            resp = client.conversations_create(name=name, is_private=private)
            channel_id = resp["channel"]["id"]
            logger.info("created channel %s (%s)", name, channel_id)
        except SlackApiError as e:
            err = e.response.get("error")
            if err == "name_taken":
                logger.info("channel %s already exists; looking it up", name)
                channel_id = _find_existing_channel_id(
                    client, name=name, private=private
                )
                if channel_id is None:
                    raise RuntimeError(
                        f"channel '{name}' reported name_taken but could not be "
                        f"found via conversations.list"
                    ) from e
                logger.info("reusing existing channel %s (%s)", name, channel_id)
            else:
                raise RuntimeError(
                    f"failed to create channel '{name}': {err}"
                ) from e

        # ---- best-effort join (bot is already in channels it created) ----
        try:
            client.conversations_join(channel=channel_id)
        except SlackApiError as e:
            logger.debug("conversations.join %s ignored: %s", name, e.response.get("error"))

        # ---- invite Ben ----
        try:
            client.conversations_invite(channel=channel_id, users=ben_user_id)
            logger.info("invited Ben to %s", name)
        except SlackApiError as e:
            err = e.response.get("error")
            if err in ("already_in_channel", "cant_invite_self"):
                logger.debug("invite to %s ignored: %s", name, err)
            else:
                logger.warning("invite to %s failed: %s", name, err)

        # ---- best-effort purpose ----
        try:
            client.conversations_setPurpose(
                channel=channel_id, purpose=_PURPOSES.get(suffix, "")
            )
        except SlackApiError as e:
            logger.debug("setPurpose %s ignored: %s", name, e.response.get("error"))

        result[f"{suffix}_channel"] = channel_id

    return result


def archive_instance_channels(
    *, bot_token: str, channel_ids: list[str]
) -> dict[str, str]:
    """Archive each non-null channel id. Returns per-channel status.

    ``already_archived`` is treated as success. Other failures are recorded but
    do not abort the batch.
    """
    client = WebClient(token=bot_token)
    status: dict[str, str] = {}
    for cid in channel_ids:
        if not cid:
            continue
        try:
            client.conversations_archive(channel=cid)
            status[cid] = "archived"
            logger.info("archived channel %s", cid)
        except SlackApiError as e:
            err = e.response.get("error")
            if err == "already_archived":
                status[cid] = "already_archived"
            else:
                status[cid] = f"error: {err}"
                logger.warning("archive %s failed: %s", cid, err)
    return status
