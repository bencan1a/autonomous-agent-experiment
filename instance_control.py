"""Per-instance operator control state (PAUSE / RESUME).

Pure stdlib. State lives in ``instances/<id>/control.json`` and records whether
the operator has paused the instance's wake cycle (e.g. for scheduled
maintenance / operator-offline windows) plus an optional one-shot "resume note"
to surface reassuringly to the agent on its next wake.

This is runtime application code (not a workflow script), so using
``datetime.now(timezone.utc)`` directly here is fine.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent
INSTANCES_DIR = AGENT_ROOT / "instances"

DEFAULT_STATE = {"paused": False, "paused_at": None, "reason": None, "resume_note": None}


def control_path(instance_id: str) -> Path:
    return INSTANCES_DIR / instance_id / "control.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_control(instance_id: str) -> dict:
    """Load the control state, tolerating a missing or corrupt file."""
    path = control_path(instance_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return dict(DEFAULT_STATE)
    if not isinstance(data, dict):
        return dict(DEFAULT_STATE)
    state = dict(DEFAULT_STATE)
    state.update({k: data.get(k, DEFAULT_STATE[k]) for k in DEFAULT_STATE})
    return state


def _write_control(instance_id: str, state: dict) -> None:
    """Atomic-ish write: temp file in the instance dir, then replace."""
    path = control_path(instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".control.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def is_paused(instance_id: str) -> bool:
    return bool(read_control(instance_id).get("paused"))


def set_paused(instance_id: str, *, reason: str | None) -> dict:
    """Mark paused; record paused_at (ISO-UTC now) and the reason. Returns new state."""
    state = read_control(instance_id)
    state["paused"] = True
    state["paused_at"] = _now_iso()
    state["reason"] = reason          # internal/operator-facing only; NEVER shown to the agent
    # Do NOT touch resume_note here — it is the agent-facing wake note, set only by resume.
    _write_control(instance_id, state)
    return dict(state)


def clear_paused(instance_id: str) -> dict:
    """Clear paused; return the PRIOR state so the caller can read paused_at.

    Leaves resume_note as whatever the caller subsequently sets (we do not
    touch it here beyond writing the new paused=False state).
    """
    prior = read_control(instance_id)
    state = dict(prior)
    state["paused"] = False
    state["paused_at"] = None
    state["reason"] = None
    _write_control(instance_id, state)
    return dict(prior)


def set_resume_note(instance_id: str, note: str | None) -> dict:
    state = read_control(instance_id)
    state["resume_note"] = note
    _write_control(instance_id, state)
    return dict(state)


def consume_resume_note(instance_id: str) -> str | None:
    """Return the resume note and clear it to None; leave the paused flag untouched."""
    state = read_control(instance_id)
    note = state.get("resume_note")
    if note is None:
        return None
    state["resume_note"] = None
    _write_control(instance_id, state)
    return note
