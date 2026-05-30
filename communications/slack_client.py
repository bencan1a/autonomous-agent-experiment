"""Slack interface for the agent.

Three lanes:
  agent_channel   — agent posts journal entries here (optional)
  observer_channel — silent mirror of every episode for Ben
  ben_dm          — direct messages to Ben (only when agent initiates)

Bot must be invited to both channels and have im:write to DM Ben.
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
        agent_channel_id: str,
        observer_channel_id: str,
        ben_user_id: str,
    ):
        self._client = WebClient(token=bot_token)
        self.agent_channel = agent_channel_id
        self.observer_channel = observer_channel_id
        self.ben_user_id = ben_user_id
        self._ben_dm_channel: str | None = None

    # ---------- auth / wiring sanity ----------

    def auth_test(self) -> dict[str, Any]:
        return self._client.auth_test().data  # type: ignore[return-value]

    # ---------- posting ----------

    def post_to_agent_channel(self, text: str) -> dict[str, Any] | None:
        return self._post(self.agent_channel, text)

    def post_to_observer_channel(self, text: str) -> dict[str, Any] | None:
        return self._post(self.observer_channel, text)

    def dm_ben(self, text: str) -> dict[str, Any] | None:
        channel = self._open_dm_with_ben()
        if channel is None:
            return None
        return self._post(channel, text)

    def fetch_dms_from_ben(self, oldest_ts: str = "0", limit: int = 50) -> list[dict[str, Any]]:
        """Return new DM messages FROM Ben (not from the bot) since oldest_ts.

        Each result: {"ts": str, "text": str, "user": str}. Sorted ascending by ts.
        Returns [] on missing scope / API failure (logged, not raised).
        Caller is responsible for persisting the newest ts after the messages
        have been delivered to the agent.
        """
        channel = self._open_dm_with_ben()
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

    def _open_dm_with_ben(self) -> str | None:
        if self._ben_dm_channel:
            return self._ben_dm_channel
        try:
            resp = self._client.conversations_open(users=self.ben_user_id)
            ch = resp["channel"]["id"]
            self._ben_dm_channel = ch
            return ch
        except SlackApiError as e:
            logger.error("conversations.open failed for %s: %s", self.ben_user_id, e.response.get("error"))
            return None

    def _post(self, channel: str, text: str) -> dict[str, Any] | None:
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
    from dotenv import load_dotenv
    load_dotenv("/home/claudebot/agent/.env")
    client = SlackClient(
        bot_token=os.environ["SLACK_BOT_TOKEN"],
        agent_channel_id=os.environ["SLACK_AGENT_CHANNEL_ID"],
        observer_channel_id=os.environ["SLACK_OBSERVER_CHANNEL_ID"],
        ben_user_id=os.environ["SLACK_BEN_USER_ID"],
    )
    info = client.auth_test()
    print("auth.test:", info)
