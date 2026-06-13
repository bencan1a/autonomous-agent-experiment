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

# control.json is a metadata SIDECAR only — it no longer carries an authoritative
# `paused` flag. The single source of truth for run-state is the registry `status`
# (see current_status / is_paused below). This file keeps pause metadata + the
# agent-facing resume note.
DEFAULT_STATE = {"paused_at": None, "reason": None, "resume_note": None}


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


def current_status(instance_id: str) -> str:
    """The instance's authoritative run-state from the registry: 'active' |
    'paused' | 'archived'. Defaults to 'paused' if the entry is unknown."""
    from instances_common import load_registry, registry_entry
    ent = registry_entry(load_registry(), instance_id)
    return (ent or {}).get("status") or "paused"


def is_paused(instance_id: str) -> bool:
    """Single source of truth: paused == registry status 'paused'. The wake gate
    and the dashboard both read this; control.json no longer carries an
    authoritative paused flag (only pause metadata + the resume note)."""
    return current_status(instance_id) == "paused"


def apply_status(instance_id: str, status: str) -> None:
    """Persist run-state to the registry (source of truth) + mirror to config.json,
    dropping the legacy redundant `active` boolean. The ONLY place run-state is
    written, so the CLI, dashboard, orchestrator, and sessions can't desync."""
    from instances_common import (
        load_instance, registry_entry, registry_txn, save_config,
    )
    with registry_txn() as reg:
        ent = registry_entry(reg, instance_id)
        if ent is not None:
            ent["status"] = status
            ent.pop("active", None)
    try:
        inst = load_instance(instance_id)
        inst.config["status"] = status
        inst.config.pop("active", None)
        save_config(instance_id, inst.config)
    except FileNotFoundError:
        pass


def pause(instance_id: str, *, reason: str | None) -> None:
    """Pause: clear any scheduled wake, set status 'paused', record pause metadata.
    The single pause path for the CLI, the dashboard button, and the session
    auto-pause (fatal / budget). NEVER shows ``reason`` to the agent."""
    import cron_control
    try:
        cron_control.remove_instance_entries(instance_id)
    except Exception:
        pass
    apply_status(instance_id, "paused")
    state = read_control(instance_id)
    state["paused_at"] = _now_iso()
    state["reason"] = reason
    _write_control(instance_id, state)


def start(instance_id: str, *, minutes_from_now: int, resume_note: str | None = None) -> None:
    """Make the instance 'active' and schedule its next wake. ADDITIVE — does not
    pause any other instance (use pause_others for exclusivity). Clears the pause
    metadata; sets the agent-facing resume_note if given."""
    import cron_control
    apply_status(instance_id, "active")
    state = read_control(instance_id)
    state["paused_at"] = None
    state["reason"] = None
    if resume_note is not None:
        state["resume_note"] = resume_note
    _write_control(instance_id, state)
    cron_control.install_instance_one_shot(instance_id, minutes_from_now=minutes_from_now)


def pause_others(keep_ids, *, reason: str | None) -> list[str]:
    """Pause every currently-active instance NOT in ``keep_ids``. Returns the ids
    paused. Backs ``activate --solo`` (opt-in exclusivity); concurrency is the
    default otherwise."""
    from instances_common import load_registry
    keep = set(keep_ids)
    paused: list[str] = []
    for iid, ent in load_registry().get("instances", {}).items():
        if iid in keep:
            continue
        if ent.get("status") == "active":
            pause(iid, reason=reason)
            paused.append(iid)
    return paused


def archive(instance_id: str) -> None:
    """Retire an instance: clear its schedule and set status 'archived'."""
    import cron_control
    try:
        cron_control.remove_instance_entries(instance_id)
    except Exception:
        pass
    apply_status(instance_id, "archived")


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
