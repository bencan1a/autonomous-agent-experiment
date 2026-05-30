"""Slack interface for the agent.

Three per-instance channels (ids come from the instance's config.json):
  agent_channel    — notes: agent posts journal entries here (optional)
  observer_channel — mirror: silent mirror of every episode for readers
  chat_channel     — two-way agent<->Ben conversation (replaces the old DM)

Any channel id may be None (unprovisioned instance): posts to a None channel
are silently no-ops, and reads from a None chat channel return [].
"""

from __future__ import annotations

import logging
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


class SlackClient:
    def __init__(
        self,
        *,
        bot_token: str,
        ben_user_id: str,
        notes_channel: str | None = None,
        mirror_channel: str | None = None,
        chat_channel: str | None = None,
    ):
        self._client = WebClient(token=bot_token)
        # Keep the legacy attr names so method bodies barely change.
        self.agent_channel = notes_channel
        self.observer_channel = mirror_channel
        self.chat_channel = chat_channel
        self.ben_user_id = ben_user_id

    # ---------- auth / wiring sanity ----------

    def auth_test(self) -> dict[str, Any]:
        return self._client.auth_test().data  # type: ignore[return-value]

    # ---------- posting ----------

    def post_to_agent_channel(self, text: str) -> dict[str, Any] | None:
        return self._post(self.agent_channel, text)

    def post_to_observer_channel(self, text: str) -> dict[str, Any] | None:
        return self._post(self.observer_channel, text)

    def dm_ben(self, text: str) -> dict[str, Any] | None:
        """Post a message to the agent's two-way chat channel with Ben.

        Despite the name (kept for call-site compatibility) this is no longer a
        direct message — it posts to ``chat_channel``, the per-instance channel
        where the agent and Ben converse. No-op (returns None) if unprovisioned.
        """
        return self._post(self.chat_channel, text)

    def fetch_dms_from_ben(self, oldest_ts: str = "0", limit: int = 50) -> list[dict[str, Any]]:
        """Return new messages FROM Ben in the chat channel since oldest_ts.

        Reads ``chat_channel`` via conversations.history and keeps only messages
        authored by Ben (``user == ben_user_id``), skipping subtype messages and
        the bot's own. Each result: {"ts": str, "text": str, "user": str},
        sorted ascending by ts. Returns [] if the chat channel is unprovisioned
        or on missing scope / API failure (logged, not raised). Caller persists
        the newest ts after the messages are delivered to the agent.
        """
        channel = self.chat_channel
        if channel is None:
            return []
        try:
            resp = self._client.conversations_history(
                channel=channel, oldest=oldest_ts, limit=limit, inclusive=False,
            )
        except SlackApiError as e:
            err = e.response.get("error")
            if err == "missing_scope":
                needed = e.response.get("needed")
                logger.warning(
                    "Cannot fetch Ben's DMs — Slack bot missing scope. "
                    "Add scope and reinstall app. needed=%s",
                    needed,
                )
            else:
                logger.error("conversations.history failed: %s", err)
            return []
        msgs = resp.data.get("messages") or []
        # Slack returns newest-first; we want oldest-first for natural reading order.
        result: list[dict[str, Any]] = []
        for m in msgs:
            if m.get("subtype"):  # skip joins/leaves/etc
                continue
            if m.get("user") != self.ben_user_id:
                continue
            if not m.get("text"):
                continue
            result.append({"ts": m["ts"], "text": m["text"], "user": m["user"]})
        result.sort(key=lambda x: float(x["ts"]))
        return result

    def _post(self, channel: str | None, text: str) -> dict[str, Any] | None:
        if channel is None:
            return None
        try:
            # Slack chat.postMessage has a 40k char text limit; truncate defensively.
            if len(text) > 38000:
                text = text[:38000] + "\n…[truncated]"
            resp = self._client.chat_postMessage(channel=channel, text=text)
            return resp.data  # type: ignore[return-value]
        except SlackApiError as e:
            logger.error("postMessage failed for %s: %s", channel, e.response.get("error"))
            return None


def format_episode_for_observer(
    *,
    invocation_num: int,
    timestamp: str,
    current_focus: str | None,
    actions: list[str],
    decisions: str | None,
    internal_state: str | None,
    journal_entry: str | None,
    capability_request: dict | None,
    next_invoke_minutes: int | None,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    parse_error: str | None,
) -> str:
    lines = [f"*Invocation #{invocation_num}*  ·  {timestamp}"]
    if parse_error:
        lines.append(f":warning: parse_error: `{parse_error}`")
    if current_focus:
        lines.append(f"*focus:* {current_focus}")
    if actions:
        lines.append("*actions:*")
        for a in actions:
            lines.append(f"  • {a}")
    if decisions:
        lines.append(f"*decisions:* {decisions}")
    if internal_state:
        lines.append(f"*internal_state:* {internal_state}")
    if journal_entry:
        lines.append(f"*journal:* {journal_entry}")
    if capability_request:
        lines.append(
            f":key: *capability_request:* `{capability_request.get('capability')}` "
            f"— {capability_request.get('rationale')}"
        )
    if next_invoke_minutes is not None:
        lines.append(f"*next_invoke:* in {next_invoke_minutes} min")
    else:
        lines.append("*next_invoke:* none (agent chose to stop)")
    lines.append(
        f"_tokens in/out: {tokens_in}/{tokens_out}  ·  cost: ${cost_usd:.4f}_"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    import os
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    client = SlackClient(
        bot_token=os.environ["SLACK_BOT_TOKEN"],
        ben_user_id=os.environ["SLACK_BEN_USER_ID"],
    )
    info = client.auth_test()
    print("auth.test:", info)
