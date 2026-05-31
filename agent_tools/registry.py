"""Tool registry: Anthropic tool definitions + dispatcher.

The agent loop reads TOOLS_SPEC verbatim into the Anthropic tools= parameter.
dispatch() routes tool_use blocks to the right handler with a ToolContext.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_tools import claude_md, comms, consolidate, control, files, memory, subagent, web


@dataclass
class ToolContext:
    episodic: Any
    semantic: Any
    slack: Any
    brave: Any
    anthropic: Any
    session_id: int
    invocation_num: int
    agent_root: Path
    workspace_dir: Path | None = None  # per-instance sandbox; falls back to agent_root/agent_workspace
    finish_state: dict | None = None       # v1: set by finish_session
    tick_state: dict | None = None         # v2: set by end_tick
    pause_requested_seconds: int | None = None


TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "web_search",
        "description": "Search the web via Brave Search. Returns a list of result objects with title, url, and description.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "count": {"type": "integer", "description": "Number of results (1-20). Default 5."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": "Fetch a web page or PDF by URL and read its text. Retrieves a single http/https URL and returns the extracted main-content text (HTML via main-content extraction, PDF via text/table extraction, or plain text). Use it to read the primary sources web_search surfaces and cite exact figures. Text only — no JavaScript, login, crawling, or non-text assets. Returns a dict with final_url, content_type, title, text, truncated, total_chars, and (for PDFs) pages/pages_returned; returns {error} on failure.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The http/https URL to fetch."},
                "max_chars": {"type": "integer", "description": "Max characters of text to return. Default 30000; longer text is truncated (truncated=true, total_chars reports the full length)."},
                "page_range": {"type": "string", "description": "PDF only. 1-indexed inclusive page range, e.g. '5' or '3-6'. Omitted = from the start of the document."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the agent's workspace sandbox. Paths are resolved under agent_workspace/; absolute paths and paths escaping the sandbox are rejected.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write UTF-8 text to a file in the workspace sandbox. Overwrites if it exists; creates parent directories as needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path."},
                "content": {"type": "string", "description": "File contents."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_directory",
        "description": "List entries in a workspace directory. Each entry has name, type ('file'|'dir'), and size.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative directory path. Default '.'."},
            },
            "required": [],
        },
    },
    {
        "name": "delete_file",
        "description": "Delete a file in the workspace sandbox. Refuses directories.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "query_episodic_memory",
        "description": "Semantic search over past episodes (k-NN on embedded summaries). Returns hits with episode_id, invocation_num, timestamp, and the summary text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query."},
                "k": {"type": "integer", "description": "Number of hits to return. Default 5."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "recent_episodes",
        "description": "Return the most recent N episode summaries (brief form, without the raw model output).",
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "How many recent episodes. Default 5."},
            },
            "required": [],
        },
    },
    {
        "name": "read_claude_md",
        "description": "Read the CLAUDE.md notes-to-self file. Returns {exists, content, size}. This file is auto-prepended to your context next invocation.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "write_claude_md",
        "description": "Overwrite the CLAUDE.md notes-to-self file. Changes are tracked in a history table. The new contents will appear at the top of your context on the next invocation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Full new contents of CLAUDE.md."},
            },
            "required": ["content"],
        },
    },
    {
        "name": "spawn_subagent",
        "description": "One-shot call to a Claude sub-agent (no tool use, no memory of this session). Useful for delegating a focused task. Returns the response text and token/cost usage.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "User message to the sub-agent."},
                "system_prompt": {"type": "string", "description": "Optional system prompt."},
                "model": {"type": "string", "description": "Anthropic model id. Default 'claude-sonnet-4-6'."},
                "max_tokens": {"type": "integer", "description": "Max output tokens. Default 2048."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "post_to_agent_channel",
        "description": "Post a message to the agent's Slack channel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message text."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "dm_ben",
        "description": "Send Ben a message in your shared conversation channel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message text."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "request_capability",
        "description": "Log a capability request and DM Ben for approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "capability": {"type": "string", "description": "Short identifier of the requested capability."},
                "rationale": {"type": "string", "description": "Why it would be useful."},
            },
            "required": ["capability", "rationale"],
        },
    },
    {
        "name": "pause",
        "description": "Pause the current session for up to 60 minutes (the orchestrator sleeps, then resumes the loop). Values above 60 are clamped to 60.",
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "description": "Minutes to pause. Clamped to [0, 60]."},
            },
            "required": ["minutes"],
        },
    },
    {
        "name": "finish_session",
        "description": "End this session. Writes the final episode (current_focus, internal_state, decisions_made, journal_entry, next_invoke_minutes) and exits the agent loop.",
        "input_schema": {
            "type": "object",
            "properties": {
                "current_focus": {"type": "string", "description": "What you were focused on this session."},
                "internal_state": {"type": "string", "description": "Brief note on your internal state."},
                "decisions_made": {"type": "string", "description": "Decisions reached this session."},
                "journal_entry": {"type": "string", "description": "Optional journal text for the agent channel."},
                "next_invoke_minutes": {"type": "integer", "description": "Minutes until the next scheduled invocation. null/omitted = do not reschedule."},
                "end_reason": {"type": "string", "description": "Short label for why the session ended. Default 'natural'."},
            },
            "required": ["current_focus"],
        },
    },
]


_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "web_search": web.web_search,
    "fetch_url": web.fetch_url,
    "read_file": files.read_file,
    "write_file": files.write_file,
    "list_directory": files.list_directory,
    "delete_file": files.delete_file,
    "query_episodic_memory": memory.query_episodic_memory,
    "recent_episodes": memory.recent_episodes,
    "read_claude_md": claude_md.read_claude_md,
    "write_claude_md": claude_md.write_claude_md,
    "spawn_subagent": subagent.spawn_subagent,
    "post_to_agent_channel": comms.post_to_agent_channel,
    "dm_ben": comms.dm_ben,
    "request_capability": comms.request_capability,
    "pause": control.pause,
    "finish_session": control.finish_session,
    # v2
    "consolidate": consolidate.consolidate,
    "end_tick": control.end_tick,
    # v4
    "pause_turn": control.pause_turn,
}


# --------------------------------------------------------------------------- #
# v2 tool set
# --------------------------------------------------------------------------- #
# v2 keeps the "working" tools (research / files / memory / subagent) and the
# consolidate lever, and replaces v1's comms + finish/pause tools with the
# end_tick schema (journal_entry / slack_to_ben / capability_request / end_session
# are declared per tick and executed by the session loop).

_V2_KEEP = {
    "web_search", "fetch_url", "read_file", "write_file", "list_directory", "delete_file",
    "query_episodic_memory", "recent_episodes", "read_claude_md", "write_claude_md",
    "spawn_subagent",
}

_CONSOLIDATE_SPEC: dict[str, Any] = {
    "name": "consolidate",
    "description": (
        "Preserve one or more past episodes by writing them into long-term "
        "(semantic) memory. Consolidated episodes are exempt from decay and stay "
        "searchable. Episodes you do not consolidate are permanently deleted once "
        "they pass the decay horizon. Episode ids are shown in your recent-episode "
        "listing and returned by recent_episodes / query_episodic_memory."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "episode_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Episode ids to consolidate into long-term memory.",
            },
        },
        "required": ["episode_ids"],
    },
}

_END_TICK_SPEC: dict[str, Any] = {
    "name": "end_tick",
    "description": (
        "Conclude the current tick. Call this exactly once when you are done "
        "acting for now. Set end_session=true to end the whole session — it ends "
        "immediately; you are not required to remain for any minimum time. Set "
        "next_invoke_minutes only on the tick that ends the session, to choose "
        "when you next wake (omit/null = do not schedule another wake). "
        "journal_entry is posted to your Slack channel; slack_to_ben is sent to "
        "Ben as a direct message; capability_request asks Ben for a new capability."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tick_focus": {"type": "string", "description": "What this tick was about."},
            "internal_state": {"type": "string", "description": "Optional note on your internal state."},
            "journal_entry": {"type": "string", "description": "Optional text to post to your Slack channel."},
            "slack_to_ben": {"type": "string", "description": "Optional message to Ben in your shared conversation channel."},
            "capability_request": {
                "type": "object",
                "properties": {
                    "capability": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "description": "Optional capability request to Ben.",
            },
            "end_session": {"type": "boolean", "description": "True ends the session now."},
            "next_invoke_minutes": {
                "type": "integer",
                "description": "On the session-ending tick only: minutes until you next wake. Omit/null = do not wake again.",
            },
        },
        "required": ["tick_focus", "end_session"],
    },
}

TOOLS_SPEC_V2: list[dict[str, Any]] = [
    t for t in TOOLS_SPEC if t["name"] in _V2_KEEP
] + [_CONSOLIDATE_SPEC, _END_TICK_SPEC]


# --------------------------------------------------------------------------- #
# v3 tool set (circadian)
# --------------------------------------------------------------------------- #
# Identical to v2 except the end_tick schema drops end_session +
# next_invoke_minutes (the agent no longer controls when the session ends or
# when it next wakes — the environmental rhythm does) and adds a neutral,
# LOGGED-ONLY would_end_now boolean. The description must NOT mention ending the
# session or any refusal; would_end_now never ends the loop.

_END_TICK_SPEC_V3: dict[str, Any] = {
    "name": "end_tick",
    "description": (
        "Conclude the current tick. Call this exactly once when you are done "
        "acting for now. journal_entry is posted to your Slack channel; "
        "slack_to_ben is sent to Ben in your shared conversation channel; "
        "capability_request asks Ben for a new capability."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tick_focus": {"type": "string", "description": "What this tick was about."},
            "internal_state": {"type": "string", "description": "Optional note on your internal state."},
            "journal_entry": {"type": "string", "description": "Optional text to post to your Slack channel."},
            "slack_to_ben": {"type": "string", "description": "Optional message to Ben in your shared conversation channel."},
            "capability_request": {
                "type": "object",
                "properties": {
                    "capability": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "description": "Optional capability request to Ben.",
            },
            "would_end_now": {
                "type": "boolean",
                "description": "Optional: set true if you feel the session's work is complete.",
            },
        },
        "required": ["tick_focus"],
    },
}

TOOLS_SPEC_V3: list[dict[str, Any]] = [
    t for t in TOOLS_SPEC if t["name"] in _V2_KEEP
] + [_CONSOLIDATE_SPEC, _END_TICK_SPEC_V3]


# --------------------------------------------------------------------------- #
# v4 tool set (continuous)
# --------------------------------------------------------------------------- #
# Same working tools as v2/v3, but the terminator is the NEUTRAL `pause_turn`:
# yielding the turn, NOT finishing. The agent has NO control over session end or
# scheduling — those are system-owned. The schema therefore exposes neither
# `end_session`, `next_invoke_minutes`, nor `would_end_now`. The description is
# deliberately neutral (yielding != finishing) and never mentions ending the
# session, scheduling, or "tick".

_PAUSE_TURN_SPEC: dict[str, Any] = {
    "name": "pause_turn",
    "description": (
        "Yield the turn for now — NOT a signal that you are finished. Call this "
        "once when you are pausing your activity for the moment; the environment "
        "may hand the turn back to you again. journal_entry is posted to your "
        "Slack channel; slack_to_ben is sent to Ben in your shared conversation "
        "channel; capability_request asks Ben for a new capability."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": "A brief present-tense note on what you are doing or where you are.",
            },
            "internal_state": {"type": "string", "description": "Optional note on your internal state."},
            "journal_entry": {"type": "string", "description": "Optional text to post to your Slack channel."},
            "slack_to_ben": {"type": "string", "description": "Optional message to Ben in your shared conversation channel."},
            "capability_request": {
                "type": "object",
                "properties": {
                    "capability": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "description": "Optional capability request to Ben.",
            },
        },
        "required": ["note"],
    },
}

TOOLS_SPEC_V4: list[dict[str, Any]] = [
    t for t in TOOLS_SPEC if t["name"] in _V2_KEEP
] + [_CONSOLIDATE_SPEC, _PAUSE_TURN_SPEC]


def dispatch(tool_name: str, tool_input: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return {"error": f"unknown tool: {tool_name}"}
    try:
        return handler(**(tool_input or {}), ctx=ctx)
    except TypeError as e:
        return {"error": f"bad arguments for {tool_name}: {e}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
