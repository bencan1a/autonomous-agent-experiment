"""Mocked tests for the per-instance Slack channel model.

Fully offline: NO real network. A fake WebClient records calls and returns
canned responses. Covers:

  - SlackClient: post_to_agent_channel/post_to_observer_channel/dm_ben target
    the correct channel ids (notes/mirror/chat); dm_ben targets CHAT; fetch
    reads the chat channel and returns only Ben's messages; None channels no-op.
  - provisioning: name_taken on one channel is resolved via conversations_list;
    Ben is invited; returns all four ids (notes/mirror/chat/advisory).
  - archive: conversations_archive called per id; already_archived ignored.

Run:
    ./venv/bin/python tests/test_slack_channels.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slack_sdk.errors import SlackApiError  # noqa: E402

import communications.slack_client as slack_client_mod  # noqa: E402
import communications.slack_provisioning as prov_mod  # noqa: E402
from communications.slack_client import SlackClient  # noqa: E402
from communications.slack_provisioning import (  # noqa: E402
    archive_instance_channels,
    provision_instance_channels,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeResponse(dict):
    """Slack SDK responses behave like dicts and expose .data / .get."""

    @property
    def data(self):
        return self


class FakeWebClient:
    """Records chat_postMessage + conversations_history; canned histories."""

    def __init__(self, *, token=None, histories=None):
        self.token = token
        self.posts = []  # list of (channel, text)
        self.history_calls = []  # list of kwargs
        self._histories = histories or {}

    def chat_postMessage(self, *, channel, text):
        self.posts.append((channel, text))
        return FakeResponse({"ok": True, "channel": channel, "ts": "1.0"})

    def conversations_history(self, *, channel, oldest="0", limit=50, inclusive=False):
        self.history_calls.append(
            {"channel": channel, "oldest": oldest, "limit": limit}
        )
        msgs = self._histories.get(channel, [])
        return FakeResponse({"ok": True, "messages": msgs})


class FakeProvClient:
    """Fake WebClient for provisioning. One channel's create raises name_taken."""

    def __init__(self, *, name_taken_for=None, existing=None):
        self.name_taken_for = name_taken_for
        self.existing = existing or {}  # name -> id (for the name_taken lookup)
        self.created = []
        self.joins = []
        self.invites = []
        self.purposes = []
        self.list_calls = 0
        self._next_id = 100

    def conversations_create(self, *, name, is_private=False):
        if name == self.name_taken_for:
            raise SlackApiError("name_taken", {"ok": False, "error": "name_taken"})
        self._next_id += 1
        cid = f"C{self._next_id}"
        self.created.append((name, cid, is_private))
        return FakeResponse({"ok": True, "channel": {"id": cid, "name": name}})

    def conversations_list(self, *, types, limit=1000, cursor=None):
        self.list_calls += 1
        channels = [{"id": cid, "name": nm} for nm, cid in self.existing.items()]
        return FakeResponse(
            {"ok": True, "channels": channels, "response_metadata": {"next_cursor": ""}}
        )

    def conversations_join(self, *, channel):
        self.joins.append(channel)
        return FakeResponse({"ok": True})

    def conversations_invite(self, *, channel, users):
        self.invites.append((channel, users))
        return FakeResponse({"ok": True})

    def conversations_setPurpose(self, *, channel, purpose):
        self.purposes.append((channel, purpose))
        return FakeResponse({"ok": True})


class FakeArchiveClient:
    def __init__(self, *, already_archived_for=None, error_for=None):
        self.already_archived_for = already_archived_for or set()
        self.error_for = error_for or {}
        self.archived = []

    def conversations_archive(self, *, channel):
        self.archived.append(channel)
        if channel in self.already_archived_for:
            raise SlackApiError(
                "already_archived", {"ok": False, "error": "already_archived"}
            )
        if channel in self.error_for:
            raise SlackApiError(
                "err", {"ok": False, "error": self.error_for[channel]}
            )
        return FakeResponse({"ok": True})


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

def scenario_client_posts_target_correct_channels():
    fake = FakeWebClient()
    orig = slack_client_mod.WebClient
    slack_client_mod.WebClient = lambda token=None: fake
    try:
        c = SlackClient(
            bot_token="x", ben_user_id="UBEN",
            notes_channel="CNOTES", mirror_channel="CMIRROR", chat_channel="CCHAT",
        )
        c.post_to_agent_channel("journal")
        c.post_to_observer_channel("mirror")
        c.dm_ben("hi ben")
    finally:
        slack_client_mod.WebClient = orig

    assert ("CNOTES", "journal") in fake.posts, fake.posts
    assert ("CMIRROR", "mirror") in fake.posts, fake.posts
    # dm_ben must target the CHAT channel, not notes/mirror.
    assert ("CCHAT", "hi ben") in fake.posts, fake.posts
    return "agent->notes, observer->mirror, dm_ben->chat"


def scenario_fetch_filters_to_bens_messages():
    histories = {
        "CCHAT": [
            {"ts": "3.0", "text": "from ben 2", "user": "UBEN"},
            {"ts": "2.0", "text": "from bot", "user": "UBOT"},
            {"ts": "1.5", "text": "join noise", "user": "UBEN", "subtype": "channel_join"},
            {"ts": "1.0", "text": "from ben 1", "user": "UBEN"},
        ]
    }
    fake = FakeWebClient(histories=histories)
    orig = slack_client_mod.WebClient
    slack_client_mod.WebClient = lambda token=None: fake
    try:
        c = SlackClient(
            bot_token="x", ben_user_id="UBEN",
            notes_channel="CNOTES", mirror_channel="CMIRROR", chat_channel="CCHAT",
        )
        msgs = c.fetch_dms_from_ben(oldest_ts="0")
    finally:
        slack_client_mod.WebClient = orig

    # Reads the chat channel.
    assert fake.history_calls and fake.history_calls[0]["channel"] == "CCHAT", fake.history_calls
    # Only Ben's non-subtype messages, ascending.
    assert [m["text"] for m in msgs] == ["from ben 1", "from ben 2"], msgs
    assert all(m["user"] == "UBEN" for m in msgs), msgs
    return "fetch reads chat channel; only Ben's non-subtype messages, ascending"


def scenario_none_channels_noop():
    fake = FakeWebClient()
    orig = slack_client_mod.WebClient
    slack_client_mod.WebClient = lambda token=None: fake
    try:
        c = SlackClient(bot_token="x", ben_user_id="UBEN")  # all channels None
        assert c.post_to_agent_channel("a") is None
        assert c.post_to_observer_channel("b") is None
        assert c.dm_ben("c") is None
        assert c._post(None, "x") is None
        assert c.fetch_dms_from_ben() == []
    finally:
        slack_client_mod.WebClient = orig

    assert fake.posts == [], fake.posts
    assert fake.history_calls == [], fake.history_calls
    return "None channels no-op: no posts, no history reads, no exceptions"


def scenario_provision_name_taken_reuse():
    fake = FakeProvClient(
        name_taken_for="inst-mirror",
        existing={"inst-mirror": "CEXIST"},
    )
    orig = prov_mod.WebClient
    prov_mod.WebClient = lambda token=None: fake
    try:
        result = provision_instance_channels(
            bot_token="x", instance_id="inst", ben_user_id="UBEN", private=False,
        )
    finally:
        prov_mod.WebClient = orig

    assert result["notes_channel"], result
    assert result["chat_channel"], result
    assert result["advisory_channel"], result
    # The name_taken channel resolved via conversations_list to the existing id.
    assert result["mirror_channel"] == "CEXIST", result
    assert fake.list_calls >= 1, "conversations_list not used for name_taken"
    # Ben invited to all four.
    invited_channels = {ch for ch, _ in fake.invites}
    assert invited_channels == {
        result["notes_channel"], result["mirror_channel"],
        result["chat_channel"], result["advisory_channel"],
    }, fake.invites
    assert all(u == "UBEN" for _, u in fake.invites), fake.invites
    # All four keys present and non-null.
    assert set(result.keys()) == {
        "notes_channel", "mirror_channel", "chat_channel", "advisory_channel",
    }
    assert all(v for v in result.values()), result
    return "name_taken reused via conversations_list; Ben invited; all 4 ids returned"


def scenario_archive_per_id_ignores_already():
    fake = FakeArchiveClient(already_archived_for={"C2"})
    orig = prov_mod.WebClient
    prov_mod.WebClient = lambda token=None: fake
    try:
        status = archive_instance_channels(
            bot_token="x", channel_ids=["C1", "C2", None, "C3"],
        )
    finally:
        prov_mod.WebClient = orig

    # Archive attempted per non-null id.
    assert fake.archived == ["C1", "C2", "C3"], fake.archived
    assert status["C1"] == "archived", status
    assert status["C2"] == "already_archived", status
    assert status["C3"] == "archived", status
    assert None not in status
    return "archive per non-null id; already_archived ignored"


SCENARIOS = [
    ("1: client posts target notes/mirror/chat", scenario_client_posts_target_correct_channels),
    ("2: fetch filters to Ben's messages", scenario_fetch_filters_to_bens_messages),
    ("3: None channels no-op", scenario_none_channels_noop),
    ("4: provision name_taken reuse + invite", scenario_provision_name_taken_reuse),
    ("5: archive per id, ignore already_archived", scenario_archive_per_id_ignores_already),
]


def _main():
    failures = 0
    for label, fn in SCENARIOS:
        try:
            detail = fn()
            print(f"PASS  {label}  —  {detail}")
        except Exception as e:
            failures += 1
            import traceback
            print(f"FAIL  {label}  —  {type(e).__name__}: {e}")
            traceback.print_exc()
    print()
    total = len(SCENARIOS)
    print(f"{total - failures}/{total} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
