"""AGENTS.md notes-to-self read/write tool.

The notes file is vendor-neutral (``AGENTS.md``) so the agent isn't told its own
self-notes are a Claude-branded file when it runs on a non-Claude model. Reads
fall back to a legacy ``CLAUDE.md``; writes always target the canonical name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from instances_common import NOTES_FILENAME, notes_path


def _workspace(ctx: Any) -> Path:
    ws = getattr(ctx, "workspace_dir", None)
    return Path(ws) if ws else ctx.agent_root / "agent_workspace"


def _read_path(ctx: Any) -> Path:
    """Resolve the file to read (prefers AGENTS.md, falls back to legacy CLAUDE.md)."""
    return notes_path(_workspace(ctx))


def _write_path(ctx: Any) -> Path:
    """Always write to the canonical name so the first write migrates forward."""
    return _workspace(ctx) / NOTES_FILENAME


def read_agents_md(*, ctx: Any) -> dict[str, Any]:
    p = _read_path(ctx)
    if not p.exists():
        return {"exists": False, "content": None, "size": 0}
    content = p.read_text(encoding="utf-8")
    return {"exists": True, "content": content, "size": len(content.encode("utf-8"))}


def write_agents_md(content: str, *, ctx: Any) -> dict[str, Any]:
    p = _write_path(ctx)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    size = len(content.encode("utf-8"))
    try:
        ctx.episodic.log_claude_md_change(ctx.session_id, content)
    except Exception:
        pass
    return {"written": True, "size": size}
