"""Verification tests for the dashboard "new instance" clone feature
(instance_manager.create_cloned_instance).

Fully offline: NO real network / Slack / Anthropic / crontab. EpisodicStore is
REAL (sqlite) pointed at a temp instances/ tree; path constants are monkeypatched
at temp scope (same harness idea as tests/test_fork.py).

Run:
    ./venv/bin/python tests/test_create_clone.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instance_manager as im  # noqa: E402
import instances_common as ic  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402


class clone_env:
    """Redirect path constants to a temp tree for the duration."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self._saved: dict = {}

    def __enter__(self):
        self._saved = {
            (ic, "INSTANCES_DIR"): ic.INSTANCES_DIR,
            (ic, "REGISTRY_PATH"): ic.REGISTRY_PATH,
            (ic, "REGISTRY_LOCK"): ic.REGISTRY_LOCK,
            (im, "AGENT_ROOT"): im.AGENT_ROOT,
        }
        ic.INSTANCES_DIR = self.tmp / "instances"
        ic.REGISTRY_PATH = self.tmp / "registry.json"
        ic.REGISTRY_LOCK = self.tmp / "registry.json.lock"
        im.AGENT_ROOT = self.tmp
        (self.tmp / "instances").mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        for (obj, attr), val in self._saved.items():
            setattr(obj, attr, val)
        return False


def _seed_parent() -> ic.Instance:
    """Create a realistic active v4 parent with a couple of distinctive config
    values so we can assert the clone inherits them."""
    pid = "v4-parent"
    cfg = ic.default_config(pid, "v4")
    cfg["status"] = "active"
    cfg["daily_cost_cap_usd"] = 42  # distinctive, non-default
    inst = ic.Instance(pid, ic.instance_dir(pid), cfg)
    inst.ensure_dirs()
    ic.save_config(pid, cfg)
    with ic.registry_txn() as reg:
        reg.setdefault("instances", {})[pid] = {
            "id": pid, "name": cfg["name"], "version": "v4",
            "status": "active", "created_at": ic.now_iso(),
            "active": True, "last_wake": None,
        }
    return ic.load_instance(pid)


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #

def scenario_happy_path_clone():
    """Config-only clone onto a new model: fresh, paused, inheriting parent cfg."""
    with tempfile.TemporaryDirectory() as td:
        with clone_env(Path(td)):
            parent = _seed_parent()
            child_id, channels = im.create_cloned_instance(
                parent, name="Probe GPT", model="openai/gpt-5.5",
                require_slack=False, validate_model=False,
            )
            assert child_id and child_id != parent.id, child_id
            assert channels == {}, channels  # no slack in test mode

            child = ic.load_instance(child_id)
            c = child.config
            # model overridden; version + distinctive budget + research inherited
            assert c["model"] == "openai/gpt-5.5", c.get("model")
            assert c["version"] == parent.version == "v4", c.get("version")
            assert c["daily_cost_cap_usd"] == 42, c.get("daily_cost_cap_usd")
            assert c["research"] == parent.config["research"], "research config must clone"
            # fresh + paused + clean root
            assert c["status"] == "paused", c.get("status")
            assert c["slack"] == {
                "notes_channel": None, "mirror_channel": None,
                "chat_channel": None, "advisory_channel": None,
            }, c.get("slack")
            for k in ("parent_id", "branch_label", "fork_group", "forked_at_invocation"):
                assert k not in c, f"lineage key {k!r} must be absent on a clone"

            # registry: paused + inactive
            ent = ic.registry_entry(ic.load_registry(), child_id)
            assert ent is not None and ent["status"] == "paused" and ent["active"] is False, ent

            # empty memory + empty workspace (no state copy)
            ep = EpisodicStore(child.episodes_db)
            assert ep.next_invocation_num() == 1, "clone must start with no episodes"
            assert not any(child.workspace_dir.iterdir()), "workspace must be empty"
    return "clone inherits version/budgets/research, overrides model, fresh+paused, empty memory/workspace"


def scenario_rollback_on_slack_failure():
    """A Slack provisioning failure must fully roll back: no dir, no registry entry."""
    with tempfile.TemporaryDirectory() as td:
        with clone_env(Path(td)):
            parent = _seed_parent()

            # creds present (so the early check passes) but provisioning explodes.
            os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
            os.environ["SLACK_BEN_USER_ID"] = "Utest"
            saved = im._provision_and_save
            im._provision_and_save = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("slack boom")
            )
            try:
                raised = False
                try:
                    im.create_cloned_instance(
                        parent, name="Rollback Probe", model="x-ai/grok-4.3",
                        require_slack=True, validate_model=False,
                    )
                except RuntimeError as e:
                    raised = True
                    assert "rolled back" in str(e), str(e)
                assert raised, "expected RuntimeError on slack failure"

                # predicted id == slug of the name; nothing must remain.
                predicted = "rollback-probe"
                assert not ic.instance_dir(predicted).exists(), "instance dir must be gone"
                reg = ic.load_registry()
                assert ic.registry_entry(reg, predicted) is None, "registry entry must be gone"
                # only the parent remains registered
                assert list(reg.get("instances", {}).keys()) == [parent.id], reg
            finally:
                im._provision_and_save = saved
                os.environ.pop("SLACK_BOT_TOKEN", None)
                os.environ.pop("SLACK_BEN_USER_ID", None)
    return "slack failure rolls back fully — no orphan dir, no registry entry"


def scenario_validation_errors():
    """Empty name/model raise before anything is created."""
    with tempfile.TemporaryDirectory() as td:
        with clone_env(Path(td)):
            parent = _seed_parent()
            for kwargs in ({"name": "", "model": "x"}, {"name": "x", "model": "  "}):
                try:
                    im.create_cloned_instance(parent, require_slack=False, **kwargs)
                    raise AssertionError(f"expected ValueError for {kwargs}")
                except ValueError:
                    pass
            # nothing extra registered
            assert list(ic.load_registry().get("instances", {}).keys()) == [parent.id]
    return "empty name/model rejected up front; no artifacts created"


def scenario_invalid_model_rejected():
    """A model slug OpenRouter doesn't serve is rejected up front (this is the
    qwen/qwen-max failure mode), creating nothing — even with validation on."""
    import openrouter_client
    with tempfile.TemporaryDirectory() as td:
        with clone_env(Path(td)):
            parent = _seed_parent()
            saved = openrouter_client.list_openrouter_model_ids
            # Pretend OpenRouter serves only this one model (no network).
            openrouter_client.list_openrouter_model_ids = lambda **k: {"openai/gpt-5.5"}
            try:
                raised = False
                try:
                    im.create_cloned_instance(
                        parent, name="Bad Slug", model="qwen/qwen-max",
                        require_slack=False, validate_model=True,
                    )
                except ValueError as e:
                    raised = True
                    assert "not available on OpenRouter" in str(e), str(e)
                assert raised, "expected ValueError for an unavailable model slug"
                # nothing created
                assert not ic.instance_dir("bad-slug").exists(), "no dir on bad slug"
                assert list(ic.load_registry().get("instances", {}).keys()) == [parent.id]
                # a served slug passes validation (no raise) — then proceeds to create
                cid, _ = im.create_cloned_instance(
                    parent, name="Good Slug", model="openai/gpt-5.5",
                    require_slack=False, validate_model=True,
                )
                assert ic.load_instance(cid).config["model"] == "openai/gpt-5.5"
            finally:
                openrouter_client.list_openrouter_model_ids = saved
    return "unavailable OpenRouter slug rejected up front; served slug passes validation"


SCENARIOS = [
    ("happy path clone", scenario_happy_path_clone),
    ("rollback on slack failure", scenario_rollback_on_slack_failure),
    ("validation errors", scenario_validation_errors),
    ("invalid model rejected", scenario_invalid_model_rejected),
]


def main() -> int:
    passed = 0
    for label, fn in SCENARIOS:
        try:
            detail = fn()
            print(f"PASS  {label}  —  {detail}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"FAIL  {label}  —  {e}")
            traceback.print_exc()
    print(f"\n{passed}/{len(SCENARIOS)} scenarios passed")
    return 0 if passed == len(SCENARIOS) else 1


if __name__ == "__main__":
    sys.exit(main())
