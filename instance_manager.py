#!/usr/bin/env python3
"""CLI for managing agent instances (create / list / lifecycle / inspect).

Lifecycle invariant: exactly one instance may be ``active`` at a time. The
registry is the source of truth for status + the single ``active`` flag, and
``config.json``'s ``status`` is kept in sync. Scheduling is delegated entirely
to ``cron_control``; instance layout/registry I/O to ``instances_common``.
"""

from __future__ import annotations

import argparse
import json
import sys

import cron_control as cron
from instances_common import (
    Instance,
    default_config,
    instance_dir,
    list_instances,
    load_instance,
    load_registry,
    new_instance_id,
    now_iso,
    registry_entry,
    save_config,
    save_registry,
    VALID_VERSIONS,
)


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


def _deactivate_others(registry: dict, keep_id: str) -> list[str]:
    """Pause + unschedule every active instance other than ``keep_id``.

    Returns the ids that were deactivated. Mutates ``registry`` in place; the
    caller is responsible for saving it.
    """
    deactivated: list[str] = []
    for iid, ent in registry.get("instances", {}).items():
        if iid == keep_id or not ent.get("active"):
            continue
        cron.clear_instance(iid)
        _set_lifecycle(registry, iid, status="paused", active=False)
        deactivated.append(iid)
    return deactivated


def _activate(registry: dict, instance_id: str, minutes_from_now: int) -> None:
    """Shared activate/resume core: become the sole active instance + schedule."""
    others = _deactivate_others(registry, instance_id)
    _set_lifecycle(registry, instance_id, status="active", active=True)
    save_registry(registry)

    cron.install_instance_one_shot(instance_id, minutes_from_now=minutes_from_now)
    nf = cron.next_fire_at(instance_id)

    if others:
        print(f"deactivated: {', '.join(others)}")
    print(f"activated '{instance_id}'")
    print(f"  next wake scheduled: {nf.isoformat() if nf else '(unknown)'}")


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

def cmd_create(args: argparse.Namespace) -> int:
    if args.version not in VALID_VERSIONS:
        return _err(f"invalid version {args.version!r}; expected one of {VALID_VERSIONS}")

    registry = load_registry()
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
    save_registry(registry)

    print(f"created instance: {instance_id}")
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
    _activate(registry, args.id, minutes)
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    registry = load_registry()
    ent = registry_entry(registry, args.id)
    if ent is None:
        return _err(f"no such instance: {args.id}")
    cron.clear_instance(args.id)
    _set_lifecycle(registry, args.id, status="paused", active=False)
    save_registry(registry)
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
    _activate(registry, args.id, cron.MIN_INTERVAL_MINUTES)
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    registry = load_registry()
    ent = registry_entry(registry, args.id)
    if ent is None:
        return _err(f"no such instance: {args.id}")
    cron.clear_instance(args.id)
    _set_lifecycle(registry, args.id, status="archived", active=False)
    save_registry(registry)
    print(f"archived '{args.id}' (data retained)")
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

    p_show = sub.add_parser("show", help="inspect an instance")
    p_show.add_argument("id", help="instance id")
    p_show.add_argument(
        "--episodes", type=int, default=5, help="number of recent episodes to show"
    )
    p_show.set_defaults(func=cmd_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
