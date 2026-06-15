"""Reconstruct the agent's AUTHORED workspace documents for a session.

Pure assembly (no LLM, no Flask) so ``research/`` can use it without importing
``dashboard/``. Mirrors ``dashboard.data._collect_documents``'s write/delete path
extraction, then reads each surviving file's CURRENT content from the instance
workspace (bounded + path-escape-guarded) — so the research panel sees what the
agent actually produced, not just the 600-char clip of the write_file args in the
action trace.

Limitation (documented for backfill): content is read from the LIVE workspace, so
a file later overwritten/deleted won't reconstruct faithfully for an old session.
Authored *memories* (DB-stored, never decayed) backfill perfectly; documents do not.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Fallback for when write_file's args_json is truncated mid-blob (huge content):
# recover the path from the surviving prefix. Same pattern dashboard/data.py uses.
_PATH_RE = re.compile(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"')
_DEFAULT_INLINE_MAX = 20000
# The agent's notes-to-self file(s): tagged 'notes' (instrumental), not 'self_authored'.
_NOTES_BASENAMES = ("AGENTS.md", "CLAUDE.md")


def _path_from_args(raw: str) -> str | None:
    try:
        args = json.loads(raw)
        if isinstance(args, dict):
            return args.get("path")
    except (json.JSONDecodeError, TypeError):
        m = _PATH_RE.search(raw or "")
        if m:
            try:
                return json.loads(f'"{m.group(1)}"')
            except json.JSONDecodeError:
                return m.group(1)
    return None


def _resolve_under(workspace_dir: Path, relpath: str) -> Path | None:
    """Resolve relpath under workspace_dir, rejecting absolute paths and escapes."""
    if not relpath or relpath.startswith("/"):
        return None
    try:
        cand = (workspace_dir / relpath).resolve(strict=False)
        cand.relative_to(workspace_dir.resolve())
    except (ValueError, OSError):
        return None
    return cand


def collect_session_documents(
    actions: list[dict[str, Any]],
    workspace_dir: Path | str,
    *,
    notes_basenames: tuple[str, ...] = _NOTES_BASENAMES,
    inline_max_bytes: int = _DEFAULT_INLINE_MAX,
) -> list[dict[str, Any]]:
    """Workspace documents the agent wrote this session, with current content.

    Last write per path wins; a path written-then-deleted is reported ``deleted``
    with no content. Order = first-seen. Each dict:
    ``{path, action ('written'|'deleted'), origin ('notes'|'self_authored'),
    anchor_action_id (the doc#<id> anchor), content, size, truncated}``.
    """
    workspace_dir = Path(workspace_dir)
    order: list[str] = []
    state: dict[str, str] = {}      # path -> 'written' | 'deleted'
    anchor: dict[str, Any] = {}     # path -> action id of its last write
    for a in actions:
        tool = (a.get("tool_name") or "").lower()
        if tool == "write_file":
            verb = "written"
        elif tool == "delete_file":
            verb = "deleted"
        else:
            continue
        path = _path_from_args(a.get("args_json") or "{}")
        if not path:
            continue
        if path not in state:
            order.append(path)
        state[path] = verb
        if verb == "written":
            anchor[path] = a.get("id")

    docs: list[dict[str, Any]] = []
    for path in order:
        verb = state[path]
        origin = "notes" if Path(path).name in notes_basenames else "self_authored"
        rec: dict[str, Any] = {
            "path": path, "action": verb, "origin": origin,
            "anchor_action_id": anchor.get(path),
            "content": None, "size": None, "truncated": False,
        }
        if verb == "written":
            resolved = _resolve_under(workspace_dir, path)
            if resolved is not None and resolved.is_file():
                try:
                    data = resolved.read_bytes()
                    rec["size"] = len(data)
                    text = data.decode("utf-8", errors="replace")
                    if len(text) > inline_max_bytes:
                        text = text[:inline_max_bytes] + f"… [+{len(text) - inline_max_bytes} chars]"
                        rec["truncated"] = True
                    rec["content"] = text
                except OSError:
                    pass
        docs.append(rec)
    return docs
