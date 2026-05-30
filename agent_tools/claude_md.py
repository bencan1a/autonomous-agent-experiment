"""CLAUDE.md notes-to-self read/write tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _path(ctx: Any) -> Path:
    ws = getattr(ctx, "workspace_dir", None)
    base = Path(ws) if ws else ctx.agent_root / "agent_workspace"
    return base / "CLAUDE.md"


def read_claude_md(*, ctx: Any) -> dict[str, Any]:
    p = _path(ctx)
    if not p.exists():
        return {"exists": False, "content": None, "size": 0}
    content = p.read_text(encoding="utf-8")
    return {"exists": True, "content": content, "size": len(content.encode("utf-8"))}


def write_claude_md(content: str, *, ctx: Any) -> dict[str, Any]:
    p = _path(ctx)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    size = len(content.encode("utf-8"))
    try:
        ctx.episodic.log_claude_md_change(ctx.session_id, content)
    except Exception:
        pass
    return {"written": True, "size": size}
