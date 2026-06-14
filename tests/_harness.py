"""Shared fakes/helpers for the v2–v5 session tests (plain-runner, no pytest).

These were copy-pasted byte-for-byte (or as clean supersets) across
test_v2/v3/v4_session.py. They are version-NEUTRAL test scaffolding: fake model
responses, a fake Slack/semantic store, a recording cron, a no-op lockfile, and a
helper to read loop-injected user text. Anything that genuinely differs per
version — the Anthropic client fake (queue vs cycling), the per-version `Patches`
context manager, `_build_instance` (each version's config knobs), and the
version-specific response builders (`end_tick_resp`, `pause_resp`, …) — stays in
each test file.

Import with the tests dir on sys.path (the test files insert it):
    from _harness import FakeBlock, FakeResp, tool_resp, FakeSlack, ...
"""

from __future__ import annotations


# --------------------------------------------------------------------------- #
# Fake model responses
# --------------------------------------------------------------------------- #

class FakeBlock:
    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type = type
        self.text = text
        self.name = name
        self.input = input
        self.id = id


class FakeUsage:
    def __init__(self, input_tokens=10, output_tokens=10,
                 cache_read_input_tokens=0, cache_creation_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class FakeResp:
    def __init__(self, content, usage=None, stop_reason="end_turn"):
        self.content = content
        self.usage = usage or FakeUsage()
        self.stop_reason = stop_reason


def text_resp(text, usage=None):
    return FakeResp([FakeBlock("text", text=text)], usage=usage,
                    stop_reason="end_turn")


def tool_resp(calls, usage=None):
    """calls: list of (name, input_dict). Auto-assign ids."""
    blocks = []
    for i, (name, inp) in enumerate(calls):
        blocks.append(FakeBlock("tool_use", name=name, input=inp,
                                id=f"toolu_{name}_{i}"))
    return FakeResp(blocks, usage=usage, stop_reason="tool_use")


# --------------------------------------------------------------------------- #
# Fake services
# --------------------------------------------------------------------------- #

class FakeSlack:
    """Records posts/DMs. `inbound_batches` (list of lists) feeds
    fetch_dms_from_ben one batch per call; omit it to behave like a silent
    channel (the v2/v3 default)."""

    def __init__(self, inbound_batches=None, **kw):
        self.agent_posts = []
        self.observer_posts = []
        self.dms = []
        self._inbound = list(inbound_batches or [])
        self.fetch_calls = 0

    def post_to_agent_channel(self, text):
        self.agent_posts.append(text)
        return True

    def post_to_observer_channel(self, text):
        self.observer_posts.append(text)
        return True

    def dm_ben(self, text):
        self.dms.append(text)
        return True

    def fetch_dms_from_ben(self, oldest_ts="0"):
        self.fetch_calls += 1
        if self._inbound:
            return self._inbound.pop(0)
        return []


class FakeSemantic:
    def __init__(self, *a, **kw):
        self.added = []

    def search(self, *a, **kw):
        return []

    def add_episode(self, **kw):
        self.added.append(kw)

    def count(self):
        return 0


class RecordingCron:
    def __init__(self):
        self.calls = []

    def remove_instance_entries(self, instance_id):
        self.calls.append(("remove_instance_entries", instance_id, {}))
        return 0

    def clear_instance(self, instance_id):
        self.calls.append(("clear_instance", instance_id, {}))

    def install_instance_one_shot(self, instance_id, minutes_from_now=None,
                                  min_minutes=None, command=None):
        self.calls.append(("install_instance_one_shot", instance_id,
                           {"minutes_from_now": minutes_from_now,
                            "min_minutes": min_minutes}))

    def names(self):
        return [c[0] for c in self.calls]

    def find(self, name):
        return [c for c in self.calls if c[0] == name]


class FakeLockfile:
    def acquire(self, path):
        return True

    def release(self, path):
        return None

    def read_pid(self, path):
        return None


# --------------------------------------------------------------------------- #
# Assertions helpers
# --------------------------------------------------------------------------- #

def _injected_user_texts(messages):
    """User-role string contents (the only place the loop injects prose). Tool
    results are list-content, so this isolates loop-injected text."""
    out = []
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            out.append(msg["content"])
    return out
