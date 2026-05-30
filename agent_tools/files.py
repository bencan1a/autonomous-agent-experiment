"""Sandboxed file tools.

All paths resolve under AGENT_ROOT / "agent_workspace". Anything escaping the
sandbox (via .., absolute paths, or symlinks) is rejected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _sandbox_root(ctx: Any) -> Path:
    ws = getattr(ctx, "workspace_dir", None)
    base = Path(ws) if ws else ctx.agent_root / "agent_workspace"
    root = base.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve(path: str, ctx: Any) -> Path:
    """Resolve a user-supplied path under the sandbox or raise ValueError."""
    root = _sandbox_root(ctx)
    p = Path(path)
    if p.is_absolute():
        candidate = p
    else:
        candidate = root / p
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as e:
        raise ValueError(f"path escapes sandbox: {path}") from e
    return resolved


def read_file(path: str, *, ctx: Any) -> dict[str, Any]:
    try:
        target = _resolve(path, ctx)
    except ValueError as e:
        return {"error": str(e)}
    if not target.exists():
        return {"error": f"file not found: {path}"}
    if not target.is_file():
        return {"error": f"not a regular file: {path}"}
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"error": f"file is not utf-8 text: {path}"}
    rel = target.relative_to(_sandbox_root(ctx))
    return {"path": str(rel), "size": target.stat().st_size, "content": content}


def write_file(path: str, content: str, *, ctx: Any) -> dict[str, Any]:
    try:
        target = _resolve(path, ctx)
    except ValueError as e:
        return {"error": str(e)}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    rel = target.relative_to(_sandbox_root(ctx))
    return {"path": str(rel), "size": target.stat().st_size, "written": True}


def list_directory(path: str = ".", *, ctx: Any) -> dict[str, Any]:
    try:
        target = _resolve(path, ctx)
    except ValueError as e:
        return {"error": str(e)}
    if not target.exists():
        return {"error": f"directory not found: {path}"}
    if not target.is_dir():
        return {"error": f"not a directory: {path}"}
    root = _sandbox_root(ctx)
    entries: list[dict[str, Any]] = []
    for child in sorted(target.iterdir()):
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "type": "dir" if child.is_dir() else "file",
            "size": stat.st_size,
        })
    return {"path": str(target.relative_to(root)) or ".", "entries": entries}


def delete_file(path: str, *, ctx: Any) -> dict[str, Any]:
    try:
        target = _resolve(path, ctx)
    except ValueError as e:
        return {"error": str(e)}
    if not target.exists():
        return {"error": f"file not found: {path}"}
    if not target.is_file():
        return {"error": f"not a regular file: {path}"}
    target.unlink()
    return {"path": path, "deleted": True}
