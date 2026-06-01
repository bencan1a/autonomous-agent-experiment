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
import os
import sys

import cron_control as cron
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
    save_config,
    save_registry,
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
