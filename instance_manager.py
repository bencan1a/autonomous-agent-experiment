#!/usr/bin/env python3
"""CLI for managing agent instances (create / list / lifecycle / inspect).

Lifecycle invariant: exactly one instance may be ``active`` at a time. The
registry is the source of truth for status + the single ``active`` flag, and
``config.json``'s ``status`` is kept in sync. Scheduling is delegated entirely
to ``cron_control``; instance layout/registry I/O to ``instances_common``.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

import cron_control as cron
import lockfile
from instances_common import (
    AGENT_ROOT,
    Instance,
    default_config,
    instance_dir,
    list_instances,
    load_instance,
    load_registry,
    new_instance_id,
    now_iso,
    registry_entry,
    registry_txn,
    save_config,
    VALID_VERSIONS,
)

# Load the shared .env so Slack token / Ben user id are available for the
# slack-provision / slack-archive subcommands. Guard if python-dotenv or the
# file is missing — the Slack subcommands will then fail with a clear message.
try:
    from dotenv import load_dotenv

    _env_path = AGENT_ROOT / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except Exception:
    pass


def _err(msg: str) -> int:
    """Print to stderr and return a nonzero exit code."""
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _sync_status(instance_id: str, status: str) -> None:
    """Mirror a status change into the instance's config.json."""
    try:
        inst = load_instance(instance_id)
    except FileNotFoundError:
        return
    inst.config["status"] = status
    save_config(instance_id, inst.config)


def _set_lifecycle(
    registry: dict, instance_id: str, *, status: str, active: bool
) -> None:
    """Update both the registry entry and config.json for one instance."""
    ent = registry_entry(registry, instance_id)
    if ent is not None:
        ent["status"] = status
        ent["active"] = active
    _sync_status(instance_id, status)


def _deactivate_unrelated(registry: dict, keep_group: set[str]) -> list[str]:
    """Pause + unschedule every active instance NOT in ``keep_group``.

    Single-active is no longer an invariant: a fork-group's branches stay
    co-active together. This deactivates only instances outside the group.
    Returns the ids deactivated. Mutates ``registry`` in place; caller saves it
    (use ``registry_txn``). Calls ``cron.clear_instance`` while the caller holds
    the registry lock — registry-before-crontab order, never the reverse.
    """
    deactivated: list[str] = []
    for iid, ent in registry.get("instances", {}).items():
        if iid in keep_group or not ent.get("active"):
            continue
        cron.clear_instance(iid)
        _set_lifecycle(registry, iid, status="paused", active=False)
        deactivated.append(iid)
    return deactivated


def _activate_group(
    registry: dict, instance_ids: list[str], minutes_from_now: int
) -> list[str]:
    """Make exactly ``instance_ids`` the active set; pause all others; schedule each.

    Mutates ``registry`` in place; the caller saves it (use ``registry_txn``).
    A group of one reproduces the old single-active behavior. Returns the ids
    that were deactivated.
    """
    keep = set(instance_ids)
    deactivated = _deactivate_unrelated(registry, keep)
    for iid in instance_ids:
        _set_lifecycle(registry, iid, status="active", active=True)
    for iid in instance_ids:
        cron.install_instance_one_shot(iid, minutes_from_now=minutes_from_now)
    return deactivated


# --------------------------------------------------------------------------- #
# slack helpers
# --------------------------------------------------------------------------- #

def _slack_creds() -> tuple[str, str]:
    """Return (bot_token, ben_user_id) from the environment or raise."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    ben = os.environ.get("SLACK_BEN_USER_ID")
    if not token or not ben:
        raise RuntimeError(
            "SLACK_BOT_TOKEN and SLACK_BEN_USER_ID must be set in .env"
        )
    return token, ben


def _provision_and_save(instance_id: str, *, private: bool) -> dict[str, str | None]:
    """Provision the three channels for an instance and persist their ids into
    the instance's config.json ``slack`` block. Returns the channel-id dict.

    Raises on missing creds or a hard Slack failure (caller reports / exits).
    """
    from communications.slack_provisioning import provision_instance_channels

    token, ben = _slack_creds()
    channels = provision_instance_channels(
        bot_token=token,
        instance_id=instance_id,
        ben_user_id=ben,
        private=private,
    )
    inst = load_instance(instance_id)
    slack_block = inst.config.get("slack")
    if not isinstance(slack_block, dict):
        slack_block = {}
    slack_block.update(channels)
    inst.config["slack"] = slack_block
    save_config(instance_id, inst.config)
    return channels


def _archive_slack_channels(instance_id: str) -> dict[str, str]:
    """Archive the three channels recorded in an instance's slack block."""
    from communications.slack_provisioning import archive_instance_channels

    token, _ = _slack_creds()
    inst = load_instance(instance_id)
    slack_block = inst.config.get("slack") or {}
    channel_ids = [
        slack_block.get("notes_channel"),
        slack_block.get("mirror_channel"),
        slack_block.get("chat_channel"),
    ]
    channel_ids = [c for c in channel_ids if c]
    return archive_instance_channels(bot_token=token, channel_ids=channel_ids)


# --------------------------------------------------------------------------- #
# fork: split a running instance into concurrent branches
# --------------------------------------------------------------------------- #

# Research tables whose rows key on experiment_id (== instance id) and so must
# be rewritten to the child id when the parent's episodes.db is copied. Every
# other research table keys on session_id / ben_contact_id and is preserved
# verbatim by the byte-for-byte copy (the session ids are unchanged). See the
# review-board audit in the plan.
_RESEARCH_EXPERIMENT_ID_TABLES = (
    "research_prereg",
    "research_cumulative",
    "research_proposals",
)


def _deep_merge(dst: dict, patch: dict) -> dict:
    """Recursively merge ``patch`` into ``dst`` in place (dicts merge, else replace)."""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _parent_is_quiesced(parent: Instance) -> tuple[bool, str]:
    """True if no live orchestrator holds the parent's lock (safe to snapshot)."""
    pid = lockfile.read_pid(parent.lock_path)
    if pid is None:
        return True, ""
    if lockfile.is_pid_alive(pid) and lockfile._pid_is_orchestrator(pid):
        return False, (
            f"parent '{parent.id}' has a live orchestrator (pid {pid}); "
            "wait for the session to finish before forking"
        )
    return True, ""


def _checkpoint_db(db_path: Path) -> None:
    """Flush any WAL into the main db file so a single-file copy is complete."""
    if not db_path.exists():
        return
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.commit()
    finally:
        con.close()


def _copy_db(src: Path, dst: Path) -> None:
    _checkpoint_db(src)
    shutil.copy2(src, dst)
    # Copy WAL/SHM siblings if present (post-checkpoint they're usually empty,
    # but copy them so the child is byte-consistent regardless of journal mode).
    for suffix in ("-wal", "-shm"):
        sib = Path(str(src) + suffix)
        if sib.exists():
            shutil.copy2(sib, Path(str(dst) + suffix))


def _rewrite_research_ids(db_path: Path, parent_id: str, child_id: str) -> None:
    """Point the experiment_id-keyed research rows at the child instance."""
    if not db_path.exists():
        return
    con = sqlite3.connect(str(db_path))
    try:
        existing = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for tbl in _RESEARCH_EXPERIMENT_ID_TABLES:
            if tbl in existing:
                con.execute(
                    f"UPDATE {tbl} SET experiment_id=? WHERE experiment_id=?",
                    (child_id, parent_id),
                )
        if "research_prereg" in existing:
            con.execute(
                "UPDATE research_prereg SET spec_source=REPLACE(spec_source, ?, ?) "
                "WHERE spec_source LIKE ?",
                (
                    f"/experiments/{parent_id}.md",
                    f"/experiments/{child_id}.md",
                    f"%/experiments/{parent_id}.md",
                ),
            )
        con.commit()
    finally:
        con.close()


def _write_child_spec(parent_id: str, child_id: str, override_spec: str | None) -> bool:
    """Create experiments/<child>.md from the parent's spec (or an override file).

    Rewrites the yaml ``experiment_id:`` line to the child id so ``load_spec``
    does not flag a mismatch. Returns False if there is no source spec to copy.
    """
    src = Path(override_spec) if override_spec else AGENT_ROOT / "experiments" / f"{parent_id}.md"
    if not src.exists():
        return False
    text = src.read_text(encoding="utf-8")
    text = re.subn(r"(?m)^(experiment_id:\s*).*$", rf"\g<1>{child_id}", text, count=1)[0]
    (AGENT_ROOT / "experiments" / f"{child_id}.md").write_text(text, encoding="utf-8")
    return True


def _materialize_branch(
    parent: Instance,
    child_id: str,
    *,
    branch_label: str,
    fork_group: str,
    forked_at_invocation: int,
    override: dict,
) -> None:
    """Create the child instance dir: snapshot copy + config + spec + primers.

    Pure local filesystem/DB work — takes no registry/cron locks. Leaves the
    child paused and unregistered; the caller registers + activates it.
    """
    cfg = copy.deepcopy(parent.config)
    cfg["name"] = override.get("name") or f"{parent.name} [{branch_label}]"
    cfg["status"] = "paused"
    cfg["slack"] = {
        "notes_channel": None, "mirror_channel": None,
        "chat_channel": None, "advisory_channel": None,
    }
    cfg["parent_id"] = parent.id
    cfg["branch_label"] = branch_label
    cfg["fork_group"] = fork_group
    cfg["forked_at_invocation"] = forked_at_invocation

    child = Instance(child_id, instance_dir(child_id), cfg)
    child.ensure_dirs()

    # Full state snapshot (gives the agent seamless continuity).
    _copy_db(parent.episodes_db, child.episodes_db)
    if parent.vectors_dir.exists():
        shutil.copytree(parent.vectors_dir, child.vectors_dir, dirs_exist_ok=True)
    if parent.workspace_dir.exists():
        shutil.copytree(parent.workspace_dir, child.workspace_dir, dirs_exist_ok=True)

    _rewrite_research_ids(child.episodes_db, parent.id, child_id)

    # Per-branch primer variants (kept outside the workspace -> not agent-visible).
    if override.get("primers"):
        pdir = child.root / "research_primers"
        pdir.mkdir(parents=True, exist_ok=True)
        rels: list[str] = []
        for srcp in override["primers"]:
            srcp = Path(srcp)
            dstp = pdir / srcp.name
            shutil.copy2(srcp, dstp)
            rels.append(str(dstp.relative_to(AGENT_ROOT)))
        cfg.setdefault("research", {})["primers"] = rels

    if override.get("config"):
        _deep_merge(cfg, override["config"])

    save_config(child_id, cfg)
    _write_child_spec(parent.id, child_id, override.get("spec"))


def fork_instance(
    parent_id: str,
    branch_labels: list[str],
    *,
    at_invocation: int | None = None,
    overrides: dict | None = None,
    freeze_parent: bool = True,
    do_slack: bool = True,
    include_mirror: bool = False,
    dry_run: bool = False,
    private: bool = False,
    minutes_from_now: int | None = None,
) -> list[str]:
    """Fork ``parent_id`` into one co-active branch per label. Returns child ids.

    Each child is a full instance that continues seamlessly from the parent at
    its current cycle: copied memory (episodes.db + vectors), workspace handoff,
    spent budget, and best-effort Slack history. Branches run concurrently; the
    parent is frozen (paused) unless ``freeze_parent=False``.

    ``overrides`` maps a branch label to ``{"config": {...patch}, "spec": path,
    "primers": [paths], "name": str}`` — anything omitted is inherited.
    """
    overrides = overrides or {}
    minutes = minutes_from_now if minutes_from_now is not None else cron.MIN_INTERVAL_MINUTES

    registry = load_registry()
    if registry_entry(registry, parent_id) is None:
        raise ValueError(f"no such instance: {parent_id}")
    parent = load_instance(parent_id)

    ok, why = _parent_is_quiesced(parent)
    if not ok:
        raise RuntimeError(why)

    from memory.episodic import EpisodicStore

    next_inv = EpisodicStore(parent.episodes_db).next_invocation_num()
    fork_point = at_invocation if at_invocation is not None else next_inv - 1
    first_cycle = next_inv  # the branch's first new cycle == its display number
    parent_label = parent.config.get("branch_label") or ""
    fork_group = f"{parent_id}@{fork_point}:{'.'.join(sorted(branch_labels))}"

    # Allocate child ids (deterministic, collision-free) against a working copy.
    working = copy.deepcopy(registry)
    plan: list[dict] = []
    for label in branch_labels:
        composed = parent_label + label
        child_id = new_instance_id(f"{parent_id}-{first_cycle}{label}", working)
        working.setdefault("instances", {})[child_id] = {}  # reserve for next alloc
        plan.append({"label": label, "composed": composed, "child_id": child_id})

    if dry_run:
        print(f"[dry-run] fork '{parent_id}' at cycle {fork_point} "
              f"(branch first cycle {first_cycle}); parent will be "
              f"{'frozen' if freeze_parent else 'kept active'}:")
        for p in plan:
            ov = overrides.get(p["label"], {})
            print(f"  - {p['child_id']}  (label {first_cycle}{p['composed']}) "
                  f"overrides={ {k: v for k, v in ov.items()} }")
        print(f"  slack: {'provision + replay' if do_slack else 'skipped'}; "
              f"fork_group={fork_group}")
        return [p["child_id"] for p in plan]

    # Quiesce the parent's schedule so no wake fires mid-copy.
    cron.clear_instance(parent_id)

    # --- local snapshot work (no locks held) ---
    for p in plan:
        _materialize_branch(
            parent, p["child_id"],
            branch_label=p["composed"], fork_group=fork_group,
            forked_at_invocation=fork_point, override=overrides.get(p["label"], {}),
        )

    # --- register + co-activate (registry lock, then crontab lock) ---
    child_ids = [p["child_id"] for p in plan]
    group = list(child_ids) + ([] if freeze_parent else [parent_id])
    with registry_txn() as reg:
        for p in plan:
            if registry_entry(reg, p["child_id"]) is not None:
                raise RuntimeError(f"child id {p['child_id']} already exists (raced)")
            reg["instances"][p["child_id"]] = {
                "id": p["child_id"],
                "name": load_instance(p["child_id"]).config.get("name", p["child_id"]),
                "version": parent.version,
                "status": "paused",
                "created_at": now_iso(),
                "active": False,
                "last_wake": (registry_entry(registry, parent_id) or {}).get("last_wake"),
                "parent_id": parent_id,
                "branch_label": p["composed"],
                "fork_group": fork_group,
                "forked_at_invocation": fork_point,
            }
        _activate_group(reg, group, minutes)

    # --- Slack: provision each child's channels + replay parent history ---
    if do_slack:
        for p in plan:
            _fork_slack(parent, p["child_id"], private=private, include_mirror=include_mirror)

    return child_ids


def _fork_slack(parent: Instance, child_id: str, *, private: bool, include_mirror: bool) -> None:
    """Provision the child's Slack channels and seed them from the parent's.

    Best-effort and non-fatal: a Slack failure leaves the child usable with
    whatever channels were created and never aborts the fork.
    """
    try:
        token, ben = _slack_creds()
        channels = _provision_and_save(child_id, private=private)
    except Exception as exc:  # noqa: BLE001 — best-effort, like cmd_create
        print(f"warning: could not provision Slack for '{child_id}': {exc}", file=sys.stderr)
        return

    from communications.slack_replay import replay_channel_history

    parent_slack = parent.config.get("slack") or {}
    # chat + notes carry observer value; mirror is high-volume (opt-in); never advisory.
    pairs = [("chat_channel", "chat"), ("notes_channel", "notes")]
    if include_mirror:
        pairs.append(("mirror_channel", "mirror"))
    for key, label in pairs:
        src = parent_slack.get(key)
        dst = channels.get(key)
        if not src or not dst:
            continue
        try:
            replay_channel_history(
                bot_token=token, src_channel_id=src, dst_channel_id=dst,
                ben_user_id=ben, header=f"_(history replayed from {parent.id} at fork)_",
            )
        except Exception as exc:  # noqa: BLE001 — replay is best-effort
            print(f"warning: replay of {label} for '{child_id}' failed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# clone: a fresh-start instance carrying another's config on a different model
# --------------------------------------------------------------------------- #

def _rollback_instance(child_id: str, channels: dict | None = None) -> None:
    """Undo a partial ``create_cloned_instance``: archive any provisioned channels,
    delete the instance dir, the cloned experiment spec, and drop the registry
    entry. Best-effort; never raises (so it is safe to call from an ``except``
    block)."""
    if channels:
        ids = [
            channels.get(k) for k in
            ("notes_channel", "mirror_channel", "chat_channel", "advisory_channel")
        ]
        ids = [c for c in ids if c]
        if ids:
            try:
                from communications.slack_provisioning import archive_instance_channels

                token, _ = _slack_creds()
                archive_instance_channels(bot_token=token, channel_ids=ids)
            except Exception:
                print(f"warning: rollback could not archive Slack channels for "
                      f"'{child_id}'", file=sys.stderr)
    try:
        shutil.rmtree(instance_dir(child_id), ignore_errors=True)
    except Exception:
        pass
    try:
        (AGENT_ROOT / "experiments" / f"{child_id}.md").unlink(missing_ok=True)
    except Exception:
        pass
    try:
        with registry_txn() as reg:
            reg.get("instances", {}).pop(child_id, None)
    except Exception:
        print(f"warning: rollback could not remove registry entry for '{child_id}'",
              file=sys.stderr)


def _validate_model_available(model: str) -> None:
    """Raise ValueError if ``model`` is an OpenRouter slug that OpenRouter does not
    serve (the failure mode that silently kills a session on its first call).

    Only a *definitive* "not in the catalogue" answer blocks creation. If the
    check itself can't run (no network, API error), we log and proceed — the
    runtime fatal-error handler is the backstop, and we don't want a flaky check
    to block all creation. Native (non-"/") models are left to the Anthropic SDK.
    """
    from openrouter_client import is_openrouter_model, list_openrouter_model_ids

    if not is_openrouter_model(model):
        return
    try:
        ids = list_openrouter_model_ids(api_key=os.environ.get("OPENROUTER_API_KEY"))
    except Exception as exc:  # noqa: BLE001 — availability check is advisory
        print(f"warning: could not verify model '{model}' against OpenRouter "
              f"({exc}); proceeding", file=sys.stderr)
        return
    if model not in ids:
        raise ValueError(
            f"model '{model}' is not available on OpenRouter — check the slug at "
            f"https://openrouter.ai/models (a wrong slug 404s and kills the session)"
        )


def _clone_approved_prereg(parent: Instance, child_id: str) -> bool:
    """Copy the parent's APPROVED coding scheme (``research_prereg``) into the
    child's DB, re-keyed to the child's ``spec_hash`` and pre-approved, so both
    instances measure with the identical, already-approved instrument (the
    cross-model comparison is then model-only, not model + rubric).

    Returns False (a clean no-op) if the parent has no approved prereg, or either
    side lacks a formal spec on file — in that case the clone operationalizes and
    approves its own scheme later, as before. Note: the child's ``spec_hash``
    differs from the parent's (the rewritten ``experiment_id`` is inside the hashed
    block), so the row must be re-keyed for the panel to reuse it.
    """
    from research.spec import load_spec
    from research.store import ResearchStore

    parent_spec = load_spec(AGENT_ROOT, parent.id)
    if parent_spec is None:
        return False
    psrc = ResearchStore(parent.episodes_db)
    prereg = psrc.get_prereg(parent.id, parent_spec.spec_hash) or psrc.latest_prereg(parent.id)
    if not prereg or prereg.get("status") != "approved":
        return False

    child_spec = load_spec(AGENT_ROOT, child_id)
    if child_spec is None:
        return False

    cdst = ResearchStore(load_instance(child_id).episodes_db)
    cdst.add_prereg(
        experiment_id=child_id,
        spec_hash=child_spec.spec_hash,
        spec_source=str(AGENT_ROOT / "experiments" / f"{child_id}.md"),
        hypotheses=prereg.get("hypotheses"),
        ivars=prereg.get("ivars"),
        dvars=prereg.get("dvars"),
        controls=prereg.get("controls"),
        success=prereg.get("success"),
        stopping=prereg.get("stopping"),
        code_vocab=prereg.get("code_vocab"),
        raw_output=prereg.get("raw_output"),
        model=prereg.get("model"),
    )
    cdst.approve_prereg(child_id, child_spec.spec_hash)
    return True


def create_cloned_instance(
    parent: Instance,
    *,
    name: str,
    model: str,
    require_slack: bool = True,
    validate_model: bool = True,
) -> tuple[str, dict]:
    """Create a NEW, fresh-start instance cloning ``parent``'s config onto ``model``.

    Config-only clone: the child inherits the parent's version, budgets, schedule
    knobs, research-panel config, the parent's ``experiments/<id>.md`` spec (so the
    dashboard shows the same description and the panel uses the same hypotheses),
    and — when the parent's coding scheme is approved — the parent's APPROVED
    ``research_prereg`` (so both instances share one identical, pre-approved
    measurement instrument). It otherwise starts with EMPTY memory and workspace
    (no episodes.db / vectors / workspace copy). It is registered PAUSED + inactive
    with no cron entry, so the currently-active instance is never disturbed.

    Transactional: validates up front (cheapest failures first, before any artifact
    is created — including that an OpenRouter ``model`` slug is actually served,
    unless ``validate_model`` is False), then registers locally and provisions
    Slack. Slack is REQUIRED unless ``require_slack`` is False (tests only). If
    provisioning — or anything after local registration — fails, the instance is
    fully rolled back (channels archived where possible, dir deleted, registry
    entry dropped) and a ``RuntimeError`` is raised. Returns ``(child_id, channels)``
    on success.

    Note: a Slack failure that aborts mid-provision can orphan the channels created
    before the failure; provisioning is idempotent (re-running reuses same-named
    channels), so a retry reclaims them.
    """
    name = (name or "").strip()
    model = (model or "").strip()
    if parent.version not in VALID_VERSIONS:
        raise ValueError(f"parent has invalid version {parent.version!r}")
    if not name:
        raise ValueError("instance name is required")
    if not model:
        raise ValueError("model is required")
    # Fail before creating any artifact: missing Slack creds, or an OpenRouter
    # slug OpenRouter doesn't serve (which would 404 and kill the session).
    if require_slack:
        _slack_creds()  # raises RuntimeError if SLACK_* env vars are unset
    if validate_model:
        _validate_model_available(model)  # raises ValueError on a known-bad slug

    # Build the cloned config: fresh root, new identity + model.
    cfg = copy.deepcopy(parent.config)
    cfg["name"] = name
    cfg["model"] = model
    cfg["status"] = "paused"
    cfg["slack"] = {
        "notes_channel": None, "mirror_channel": None,
        "chat_channel": None, "advisory_channel": None,
    }
    # Strip fork lineage so the clone is a clean root, not a branch.
    for lineage_key in ("parent_id", "branch_label", "fork_group", "forked_at_invocation"):
        cfg.pop(lineage_key, None)

    # Register locally (id + dirs + config + registry entry), paused/inactive.
    with registry_txn() as registry:
        child_id = new_instance_id(name, registry)
        if registry_entry(registry, child_id) is not None:
            raise ValueError(f"instance '{child_id}' already exists")
        child = Instance(child_id, instance_dir(child_id), cfg)
        child.ensure_dirs()
        save_config(child_id, cfg)
        registry.setdefault("instances", {})[child_id] = {
            "id": child_id,
            "name": name,
            "version": parent.version,
            "status": "paused",
            "created_at": now_iso(),
            "active": False,
            "last_wake": None,
        }

    # Clone the experiment spec + provision Slack OUTSIDE the registry lock; roll
    # back everything on any failure. The spec copy rewrites experiment_id to the
    # child (no-ops cleanly if the parent has no spec on file).
    channels: dict = {}
    try:
        _write_child_spec(parent.id, child_id, None)
        _clone_approved_prereg(parent, child_id)  # share the approved instrument
        if require_slack:
            channels = _provision_and_save(child_id, private=False)
            missing = [
                k for k in ("notes_channel", "mirror_channel", "chat_channel")
                if not channels.get(k)
            ]
            if missing:
                raise RuntimeError(
                    f"Slack provisioning returned no id for: {', '.join(missing)}"
                )
    except Exception as exc:
        _rollback_instance(child_id, channels)
        raise RuntimeError(
            f"instance creation failed and was rolled back: {exc}"
        ) from exc

    return child_id, channels


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

def cmd_create(args: argparse.Namespace) -> int:
    if args.version not in VALID_VERSIONS:
        return _err(f"invalid version {args.version!r}; expected one of {VALID_VERSIONS}")

    with registry_txn() as registry:
        instance_id = new_instance_id(args.name, registry)
        if registry_entry(registry, instance_id) is not None:
            return _err(f"instance '{instance_id}' already exists")

        config = default_config(
            args.name, args.version, model=args.model, status="paused"
        )

        inst = Instance(instance_id, instance_dir(instance_id), config)
        inst.ensure_dirs()
        save_config(instance_id, config)

        registry.setdefault("instances", {})[instance_id] = {
            "id": instance_id,
            "name": args.name,
            "version": args.version,
            "status": "paused",
            "created_at": now_iso(),
            "active": False,
            "last_wake": None,
        }

    print(f"created instance: {instance_id}")

    # Best-effort: provision the three Slack channels now. If scopes aren't yet
    # granted (or any other Slack error), the instance is still created with
    # null channel ids and the operator can run slack-provision later.
    try:
        channels = _provision_and_save(instance_id, private=False)
        print("provisioned Slack channels:")
        for k, v in channels.items():
            print(f"  {k}: {v}")
    except Exception as exc:  # noqa: BLE001 — provisioning is best-effort here
        print(
            f"warning: could not provision Slack channels: {exc}\n"
            "  The instance was created with null channels. To provision later:\n"
            "    1. Ensure the Slack app has scopes: channels:manage, channels:history\n"
            "       (plus groups:write/groups:history for private channels), then reinstall the app.\n"
            f"    2. Run: instance_manager.py slack-provision {instance_id}",
            file=sys.stderr,
        )
    return 0


def _parse_overrides(path: str | None) -> dict:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("overrides file must be a JSON object mapping label -> patch")
    return data


def cmd_fork(args: argparse.Namespace) -> int:
    labels = [s.strip() for s in args.branches.split(",") if s.strip()]
    if not labels:
        return _err("--branches must list at least one label, e.g. --branches a,b")
    if len(set(labels)) != len(labels):
        return _err("--branches labels must be unique")
    try:
        overrides = _parse_overrides(args.overrides)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _err(f"could not read --overrides: {exc}")
    try:
        child_ids = fork_instance(
            args.id, labels,
            at_invocation=args.at_invocation,
            overrides=overrides,
            freeze_parent=not args.keep_parent,
            do_slack=not args.no_slack,
            include_mirror=args.include_mirror,
            dry_run=args.dry_run,
            private=args.private,
        )
    except (ValueError, RuntimeError) as exc:
        return _err(str(exc))
    if args.dry_run:
        return 0
    print(f"forked '{args.id}' -> {', '.join(child_ids)} (co-active)")
    print(f"  parent '{args.id}' {'kept active' if args.keep_parent else 'frozen (paused)'}")
    for cid in child_ids:
        nf = cron.next_fire_at(cid)
        print(f"  {cid} next wake: {nf.isoformat() if nf else '(unknown)'}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = list_instances()
    header = ("ID", "NAME", "VERSION", "STATUS", "ACTIVE", "LAST WAKE")
    fmt = "{:<20} {:<20} {:<8} {:<9} {:<7} {:<25}"
    print(fmt.format(*header))
    print(fmt.format(*("-" * len(h) for h in header)))
    for ent in rows:
        active = "*" if ent.get("active") else ""
        print(fmt.format(
            str(ent.get("id", ""))[:20],
            str(ent.get("name", ""))[:20],
            str(ent.get("version", "")),
            str(ent.get("status", "")),
            active,
            str(ent.get("last_wake") or "-"),
        ))
    return 0


def cmd_activate(args: argparse.Namespace) -> int:
    registry = load_registry()
    ent = registry_entry(registry, args.id)
    if ent is None:
        return _err(f"no such instance: {args.id}")
    if ent.get("status") == "archived" and not args.include_archived:
        return _err(
            f"instance '{args.id}' is archived; pass --include-archived to "
            "reactivate it (it will transition archived -> active)"
        )
    minutes = args.in_minutes if args.in_minutes is not None else cron.MIN_INTERVAL_MINUTES
    with registry_txn() as reg:
        deactivated = _activate_group(reg, [args.id], minutes)
    _report_activation([args.id], deactivated)
    return 0


def _report_activation(activated: list[str], deactivated: list[str]) -> None:
    if deactivated:
        print(f"deactivated: {', '.join(deactivated)}")
    print(f"activated: {', '.join(activated)}")
    for iid in activated:
        nf = cron.next_fire_at(iid)
        print(f"  {iid} next wake: {nf.isoformat() if nf else '(unknown)'}")


def cmd_pause(args: argparse.Namespace) -> int:
    registry = load_registry()
    if registry_entry(registry, args.id) is None:
        return _err(f"no such instance: {args.id}")
    cron.clear_instance(args.id)
    with registry_txn() as reg:
        _set_lifecycle(reg, args.id, status="paused", active=False)
    print(f"paused '{args.id}' (data retained, schedule cleared)")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    registry = load_registry()
    ent = registry_entry(registry, args.id)
    if ent is None:
        return _err(f"no such instance: {args.id}")
    if ent.get("status") == "archived":
        return _err(
            f"instance '{args.id}' is archived; cannot resume an archived instance"
        )
    with registry_txn() as reg:
        deactivated = _activate_group(reg, [args.id], cron.MIN_INTERVAL_MINUTES)
    _report_activation([args.id], deactivated)
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    registry = load_registry()
    if registry_entry(registry, args.id) is None:
        return _err(f"no such instance: {args.id}")
    cron.clear_instance(args.id)
    with registry_txn() as reg:
        _set_lifecycle(reg, args.id, status="archived", active=False)
    print(f"archived '{args.id}' (data retained)")

    # Best-effort: archive the instance's Slack channels too.
    try:
        _archive_slack_channels(args.id)
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"warning: could not archive Slack channels: {exc}", file=sys.stderr)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    registry = load_registry()
    ent = registry_entry(registry, args.id)
    if ent is None:
        return _err(f"no such instance: {args.id}")

    try:
        inst = load_instance(args.id)
    except FileNotFoundError as exc:
        return _err(str(exc))

    print("=== config.json ===")
    print(json.dumps(inst.config, indent=2))

    print("\n=== registry entry ===")
    print(json.dumps(ent, indent=2))

    print("\n=== cron ===")
    entries = cron.current_instance_entries(args.id)
    if entries:
        for ln in entries:
            print(f"  {ln}")
    else:
        print("  (no cron entry)")
    nf = cron.next_fire_at(args.id)
    print(f"  next fire: {nf.isoformat() if nf else '(none)'}")

    print(f"\n=== last {args.episodes} episode(s) ===")
    db_path = inst.episodes_db
    if not db_path.exists():
        print("  (no episodes.db yet)")
        return 0
    try:
        from memory.episodic import EpisodicStore

        store = EpisodicStore(db_path)
        episodes = store.recent_episodes(args.episodes)
    except Exception as exc:  # noqa: BLE001 - read-only inspection, never fatal
        print(f"  (could not read episodes: {exc})")
        return 0

    if not episodes:
        print("  (no episodes recorded)")
        return 0

    for ep in episodes:
        ts = ep.get("timestamp", "?")
        num = ep.get("invocation_num", "?")
        focus = ep.get("current_focus") or "-"
        cost = ep.get("cost_usd") or 0.0
        print(f"  #{num} [{ts}] ${cost:.4f}  {focus}")
    return 0


def cmd_slack_provision(args: argparse.Namespace) -> int:
    registry = load_registry()
    if registry_entry(registry, args.id) is None:
        return _err(f"no such instance: {args.id}")
    try:
        load_instance(args.id)
    except FileNotFoundError as exc:
        return _err(str(exc))
    try:
        channels = _provision_and_save(args.id, private=args.private)
    except Exception as exc:  # noqa: BLE001
        return _err(
            f"Slack provisioning failed: {exc}\n"
            "  Ensure the Slack app has scopes channels:manage, channels:history "
            "(and groups:write/groups:history for private channels), then reinstall "
            "the app and retry."
        )
    print(f"provisioned Slack channels for '{args.id}':")
    for k, v in channels.items():
        print(f"  {k}: {v}")
    return 0


def cmd_slack_archive(args: argparse.Namespace) -> int:
    registry = load_registry()
    if registry_entry(registry, args.id) is None:
        return _err(f"no such instance: {args.id}")
    try:
        load_instance(args.id)
    except FileNotFoundError as exc:
        return _err(str(exc))
    try:
        status = _archive_slack_channels(args.id)
    except Exception as exc:  # noqa: BLE001
        return _err(f"Slack archive failed: {exc}")
    if not status:
        print(f"no Slack channels recorded for '{args.id}' (nothing to archive)")
        return 0
    print(f"archived Slack channels for '{args.id}':")
    for cid, st in status.items():
        print(f"  {cid}: {st}")
    return 0


def _research_store_and_prereg(instance_id: str):
    """Return (instance, ResearchStore, spec, prereg) or an error code.

    The prereg returned is the one matching the *current* formal spec when a row
    for it exists, otherwise the latest. Returns an int (error code) on failure.
    """
    registry = load_registry()
    if registry_entry(registry, instance_id) is None:
        return _err(f"no such instance: {instance_id}")
    try:
        inst = load_instance(instance_id)
    except FileNotFoundError as exc:
        return _err(str(exc))
    from research.spec import load_spec
    from research.store import ResearchStore

    spec = load_spec(AGENT_ROOT, instance_id)
    rs = ResearchStore(inst.episodes_db)
    prereg = None
    if spec is not None:
        prereg = rs.get_prereg(instance_id, spec.spec_hash)
    if prereg is None:
        prereg = rs.latest_prereg(instance_id)
    return inst, rs, spec, prereg


def cmd_research_show(args: argparse.Namespace) -> int:
    got = _research_store_and_prereg(args.id)
    if isinstance(got, int):
        return got
    inst, rs, spec, prereg = got
    if spec is None:
        print(f"'{args.id}': no formal spec block in experiments/{args.id}.md "
              "(panel will no-op until one is authored).")
    else:
        print(f"'{args.id}': formal spec v{spec.spec_version}, "
              f"{len(spec.hypotheses)} hypotheses, spec_hash={spec.spec_hash[:12]}")
    if prereg is None:
        print("  no coding scheme operationalized yet (run a session).")
        return 0
    print(f"  coding scheme: status={prereg.get('status')} "
          f"(spec_hash={str(prereg.get('spec_hash'))[:12]})")
    for c in prereg.get("code_vocab") or []:
        maps = f" [{c.get('maps_to_hypothesis')}]" if c.get("maps_to_hypothesis") else ""
        print(f"    - {c.get('code')}{maps}: {c.get('definition')}")
    return 0


def cmd_research_approve(args: argparse.Namespace) -> int:
    got = _research_store_and_prereg(args.id)
    if isinstance(got, int):
        return got
    inst, rs, spec, prereg = got
    if prereg is None:
        return _err(
            f"no coding scheme to approve for '{args.id}' yet — run a session so the "
            "panel operationalizes the spec (and confirm experiments/"
            f"{args.id}.md has a formal spec block)."
        )
    if prereg.get("status") == "approved":
        print(f"'{args.id}': coding scheme already approved.")
        return 0
    print(f"Approving this coding scheme for '{args.id}':")
    for c in prereg.get("code_vocab") or []:
        print(f"    - {c.get('code')}: {c.get('definition')}")
    if rs.approve_prereg(args.id, prereg.get("spec_hash")):
        print("approved — new per-session research notes will be marked 'binding'.")
        return 0
    return _err("approval did not match any coding-scheme row.")


def cmd_research_synthesize(args: argparse.Namespace) -> int:
    registry = load_registry()
    if registry_entry(registry, args.id) is None:
        return _err(f"no such instance: {args.id}")
    try:
        inst = load_instance(args.id)
    except FileNotFoundError as exc:
        return _err(str(exc))
    # Shell may export ANTHROPIC_API_KEY=""; force it from .env (as the runners do).
    try:
        from dotenv import load_dotenv as _ld
        _ld(AGENT_ROOT / ".env", override=True)
    except Exception:
        pass
    import os as _os

    import anthropic
    from memory.episodic import EpisodicStore
    from research.store import ResearchStore
    from research.synthesis import run_cumulative_synthesis

    client = anthropic.Anthropic(api_key=_os.environ["ANTHROPIC_API_KEY"])
    ep = EpisodicStore(inst.episodes_db)
    rs = ResearchStore(inst.episodes_db)
    semantic = None
    if not args.no_embed:
        from memory.semantic import SemanticStore
        semantic = SemanticStore(inst.vectors_dir)
    print(f"Refreshing cumulative synthesis for '{inst.id}' (real API spend)...")
    res = run_cumulative_synthesis(
        instance=inst, episodic=ep, research_store=rs,
        anthropic_client=client, agent_root=AGENT_ROOT, semantic=semantic,
    )
    if not res:
        print("  no report produced (no spec / no coding scheme / disabled).")
        return 0
    print(f"  {res.get('status')} · cost=${res.get('cost_usd', 0):.4f} "
          f"drift_flagged={res.get('drift_flagged')}"
          f"{' [degraded]' if res.get('degraded') else ''}")
    return 0


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="instance_manager",
        description="Manage agent instances.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="create a new (paused) instance")
    p_create.add_argument("--name", required=True, help="human-friendly name")
    p_create.add_argument(
        "--version", required=True, choices=list(VALID_VERSIONS), help="instance version"
    )
    p_create.add_argument("--model", default=None, help="override default model")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list", help="list all instances")
    p_list.set_defaults(func=cmd_list)

    p_fork = sub.add_parser(
        "fork", help="fork a running instance into concurrent branches (8a, 8b, ...)"
    )
    p_fork.add_argument("id", help="parent instance id")
    p_fork.add_argument(
        "--branches", required=True,
        help="comma-separated branch labels, e.g. a,b",
    )
    p_fork.add_argument(
        "--at-invocation", type=int, default=None, dest="at_invocation",
        help="label-only fork point N (default: the parent's last completed cycle)",
    )
    p_fork.add_argument(
        "--overrides", default=None,
        help="JSON file mapping label -> {config, spec, primers, name} per-branch overrides",
    )
    p_fork.add_argument(
        "--keep-parent", action="store_true",
        help="leave the parent active and co-running (default: freeze/pause it)",
    )
    p_fork.add_argument(
        "--include-mirror", action="store_true",
        help="also replay the high-volume -mirror channel (default: chat + notes only)",
    )
    p_fork.add_argument(
        "--no-slack", action="store_true",
        help="skip Slack channel provisioning + history replay",
    )
    p_fork.add_argument(
        "--private", action="store_true", help="create private Slack channels",
    )
    p_fork.add_argument(
        "--dry-run", action="store_true",
        help="print the fork plan without copying/registering/scheduling anything",
    )
    p_fork.set_defaults(func=cmd_fork)

    p_activate = sub.add_parser("activate", help="make an instance the sole active one")
    p_activate.add_argument("id", help="instance id")
    p_activate.add_argument(
        "--in-minutes", type=int, default=None, dest="in_minutes",
        help=f"minutes until first wake (default {cron.MIN_INTERVAL_MINUTES})",
    )
    p_activate.add_argument(
        "--include-archived", action="store_true",
        help="allow reactivating an archived instance (archived -> active)",
    )
    p_activate.set_defaults(func=cmd_activate)

    p_pause = sub.add_parser("pause", help="pause an instance (clear its schedule)")
    p_pause.add_argument("id", help="instance id")
    p_pause.set_defaults(func=cmd_pause)

    p_resume = sub.add_parser("resume", help="resume a paused instance (== activate)")
    p_resume.add_argument("id", help="instance id")
    p_resume.set_defaults(func=cmd_resume)

    p_archive = sub.add_parser("archive", help="archive an instance (data retained)")
    p_archive.add_argument("id", help="instance id")
    p_archive.set_defaults(func=cmd_archive)

    p_provision = sub.add_parser(
        "slack-provision", help="create/reuse the instance's three Slack channels"
    )
    p_provision.add_argument("id", help="instance id")
    p_provision.add_argument(
        "--private", action="store_true", help="create private channels"
    )
    p_provision.set_defaults(func=cmd_slack_provision)

    p_archive_slack = sub.add_parser(
        "slack-archive", help="archive the instance's Slack channels"
    )
    p_archive_slack.add_argument("id", help="instance id")
    p_archive_slack.set_defaults(func=cmd_slack_archive)

    p_show = sub.add_parser("show", help="inspect an instance")
    p_show.add_argument("id", help="instance id")
    p_show.add_argument(
        "--episodes", type=int, default=5, help="number of recent episodes to show"
    )
    p_show.set_defaults(func=cmd_show)

    p_research = sub.add_parser(
        "research", help="research panel: review/approve the operationalized coding scheme"
    )
    rsub = p_research.add_subparsers(dest="research_cmd", required=True)
    p_r_show = rsub.add_parser("show", help="show the formal spec + coding scheme + status")
    p_r_show.add_argument("id", help="instance id")
    p_r_show.set_defaults(func=cmd_research_show)
    p_r_approve = rsub.add_parser(
        "approve", help="approve the coding scheme (makes per-session notes 'binding')"
    )
    p_r_approve.add_argument("id", help="instance id")
    p_r_approve.set_defaults(func=cmd_research_approve)
    p_r_syn = rsub.add_parser(
        "synthesize", help="refresh the rolling cumulative report (real API spend)"
    )
    p_r_syn.add_argument("id", help="instance id")
    p_r_syn.add_argument("--no-embed", action="store_true",
                         help="skip embedding the report summary")
    p_r_syn.set_defaults(func=cmd_research_synthesize)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
