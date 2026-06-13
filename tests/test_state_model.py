"""Verification tests for the unified instance run-state model.

Single source of truth = registry `status` ('active'|'paused'|'archived'); the
legacy `active` boolean and the control.json `paused` flag are gone. Transitions
(pause/start/pause_others) live in instance_control; reconcile() in
instance_manager migrates divergent state.

Fully offline: path constants are monkeypatched to a temp tree and cron is stubbed
(no real crontab). Run:
    ./venv/bin/python tests/test_state_model.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cron_control  # noqa: E402
import instance_control as icon  # noqa: E402
import instance_manager as im  # noqa: E402
import instances_common as ic  # noqa: E402


class env:
    """Redirect path constants to a temp tree and stub cron for the duration."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self._saved: dict = {}
        self.cron_calls: list = []

    def __enter__(self):
        self._saved = {
            (ic, "INSTANCES_DIR"): ic.INSTANCES_DIR,
            (ic, "REGISTRY_PATH"): ic.REGISTRY_PATH,
            (ic, "REGISTRY_LOCK"): ic.REGISTRY_LOCK,
            (icon, "INSTANCES_DIR"): icon.INSTANCES_DIR,
            (im, "AGENT_ROOT"): im.AGENT_ROOT,
            (cron_control, "install_instance_one_shot"): cron_control.install_instance_one_shot,
            (cron_control, "remove_instance_entries"): cron_control.remove_instance_entries,
            (cron_control, "clear_instance"): getattr(cron_control, "clear_instance", None),
        }
        ic.INSTANCES_DIR = self.tmp / "instances"
        ic.REGISTRY_PATH = self.tmp / "registry.json"
        ic.REGISTRY_LOCK = self.tmp / "registry.json.lock"
        icon.INSTANCES_DIR = self.tmp / "instances"
        im.AGENT_ROOT = self.tmp
        (self.tmp / "instances").mkdir(parents=True, exist_ok=True)

        def _install(instance_id, minutes_from_now=None):
            self.cron_calls.append(("install", instance_id, minutes_from_now))

        def _remove(instance_id):
            self.cron_calls.append(("remove", instance_id))
            return 0

        cron_control.install_instance_one_shot = _install
        cron_control.remove_instance_entries = _remove
        cron_control.clear_instance = _remove
        return self

    def __exit__(self, *exc):
        for (obj, attr), val in self._saved.items():
            if val is None:
                if hasattr(obj, attr):
                    delattr(obj, attr)
            else:
                setattr(obj, attr, val)
        return False


def _seed(iid: str, *, status: str, active=None, control: dict | None = None) -> None:
    cfg = ic.default_config(iid, "v4")
    cfg["status"] = status
    inst = ic.Instance(iid, ic.instance_dir(iid), cfg)
    inst.ensure_dirs()
    ic.save_config(iid, cfg)
    with ic.registry_txn() as reg:
        ent = {"id": iid, "name": iid, "version": "v4", "status": status,
               "created_at": ic.now_iso(), "last_wake": None}
        if active is not None:
            ent["active"] = active
        reg.setdefault("instances", {})[iid] = ent
    if control is not None:
        icon.control_path(iid).write_text(json.dumps(control) + "\n", encoding="utf-8")


def _registry_entry(iid: str) -> dict:
    return ic.registry_entry(ic.load_registry(), iid) or {}


# --------------------------------------------------------------------------- #

def scenario_pause():
    with tempfile.TemporaryDirectory() as td:
        with env(Path(td)) as e:
            _seed("a", status="active")
            icon.pause("a", reason="maint")
            assert icon.current_status("a") == "paused"
            assert icon.is_paused("a") is True
            assert _registry_entry("a")["status"] == "paused"
            # config.json mirrored
            assert ic.load_instance("a").config["status"] == "paused"
            # cron cleared
            assert ("remove", "a") in e.cron_calls, e.cron_calls
            # sidecar metadata written, no authoritative `paused` key needed
            ctrl = icon.read_control("a")
            assert ctrl["reason"] == "maint" and ctrl["paused_at"], ctrl
    return "pause sets status=paused everywhere (registry+config), clears cron, records metadata"


def scenario_start():
    with tempfile.TemporaryDirectory() as td:
        with env(Path(td)) as e:
            _seed("a", status="paused", control={"paused_at": "t", "reason": "x", "resume_note": None})
            icon.start("a", minutes_from_now=5, resume_note="welcome back")
            assert icon.current_status("a") == "active"
            assert icon.is_paused("a") is False
            assert ("install", "a", 5) in e.cron_calls, e.cron_calls
            ctrl = icon.read_control("a")
            assert ctrl["paused_at"] is None and ctrl["reason"] is None, ctrl
            assert icon.consume_resume_note("a") == "welcome back"
            assert icon.consume_resume_note("a") is None  # consumed once
    return "start sets status=active, schedules wake, clears pause meta, sets one-shot resume note"


def scenario_additive_and_solo():
    with tempfile.TemporaryDirectory() as td:
        with env(Path(td)):
            _seed("a", status="active")
            _seed("b", status="active")
            # additive: activating a leaves b running
            deact = im._activate_many(["a"], 30, solo=False)
            assert deact == [], deact
            assert icon.current_status("b") == "active"
            # solo: now pause everyone but a
            deact = im._activate_many(["a"], 30, solo=True)
            assert deact == ["b"], deact
            assert icon.current_status("b") == "paused"
            assert icon.current_status("a") == "active"
    return "activation is additive by default; --solo pauses the others"


def scenario_reconcile():
    with tempfile.TemporaryDirectory() as td:
        with env(Path(td)):
            # the bug: registry active+True but control says paused
            _seed("diverged", status="active", active=True,
                  control={"paused": True, "paused_at": "2026-06-13T03:13:00+00:00",
                           "reason": "operator maintenance pause", "resume_note": None})
            _seed("genuinely_active", status="active", active=True)
            _seed("retired", status="archived", active=False)

            rows = im.reconcile_state()
            by = {r["id"]: r for r in rows}
            assert by["diverged"]["new_status"] == "paused", by["diverged"]
            assert by["genuinely_active"]["new_status"] == "active", by["genuinely_active"]
            assert by["retired"]["new_status"] == "archived", by["retired"]
            # registry `active` boolean dropped everywhere
            for iid in ("diverged", "genuinely_active", "retired"):
                assert "active" not in _registry_entry(iid), iid
            # sidecar `paused` key stripped, but paused_at preserved
            raw = json.loads(icon.control_path("diverged").read_text())
            assert "paused" not in raw and raw.get("paused_at"), raw
            # idempotent
            rows2 = im.reconcile_state()
            assert {r["id"]: r["new_status"] for r in rows2} == \
                   {r["id"]: r["new_status"] for r in rows}
    return "reconcile maps active+control-paused -> paused; drops `active`+sidecar `paused`; idempotent"


SCENARIOS = [
    ("1: pause unifies status", scenario_pause),
    ("2: start activates + schedules", scenario_start),
    ("3: additive activation + --solo", scenario_additive_and_solo),
    ("4: reconcile migrates divergent state", scenario_reconcile),
]


def test_pause(): scenario_pause()
def test_start(): scenario_start()
def test_additive_and_solo(): scenario_additive_and_solo()
def test_reconcile(): scenario_reconcile()


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
    print(f"{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
