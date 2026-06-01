"""Shared helpers for the multi-instance agent system.

One process == one instance. Everything an instance owns lives under
``instances/<id>/``; code (memory/, tools/, dashboard/, ...) and the
embedding-model cache (``data/hf_cache/``) are shared.

Layout::

    AGENT_ROOT/
      instances/<id>/
        config.json            # per-instance params (model, schedule, budget, decay)
        data/
          episodes.db          # SQLite episodic store
          vectors/             # LanceDB semantic index
          orchestrator.lock    # per-instance single-run lock
        workspace/             # the agent's sandbox (CLAUDE.md, foundation.md, ...)
        logs/orchestrator.log
      registry.json            # id -> {name, version, status, created_at, active, last_wake}
      data/hf_cache/           # SHARED embedding-model cache (never per-instance)

The registry is the source of truth for lifecycle (status + the single
``active`` flag). ``config.json`` mirrors ``status`` for convenience and holds
the runtime params. ``instance_manager`` keeps the two in sync.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AGENT_ROOT = Path(__file__).resolve().parent
INSTANCES_DIR = AGENT_ROOT / "instances"
REGISTRY_PATH = AGENT_ROOT / "registry.json"
# Shared HuggingFace cache for the embedding model — NOT per-instance.
SHARED_HF_CACHE = AGENT_ROOT / "data" / "hf_cache"

UTC = timezone.utc

VALID_STATUSES = ("active", "paused", "archived")
VALID_VERSIONS = ("v1", "v2", "v3", "v4")

DEFAULT_MODEL_V1 = "claude-opus-4-7"
DEFAULT_MODEL_V2 = "claude-sonnet-4-6"
DEFAULT_MODEL_V3 = "claude-opus-4-8"
DEFAULT_MODEL_V4 = "claude-opus-4-8"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Instance ids
# --------------------------------------------------------------------------- #

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.lower()).strip("-")
    return s or "instance"


def new_instance_id(name: str, registry: dict) -> str:
    """A human-friendly, collision-free id derived from the name."""
    base = slugify(name)
    existing = set(registry.get("instances", {}).keys())
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


# --------------------------------------------------------------------------- #
# Instance handle
# --------------------------------------------------------------------------- #

@dataclass
class Instance:
    id: str
    root: Path
    config: dict

    # ---- paths ----
    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def episodes_db(self) -> Path:
        return self.root / "data" / "episodes.db"

    @property
    def vectors_dir(self) -> Path:
        return self.root / "data" / "vectors"

    @property
    def workspace_dir(self) -> Path:
        return self.root / "workspace"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def log_file(self) -> Path:
        return self.root / "logs" / "orchestrator.log"

    @property
    def lock_path(self) -> Path:
        return self.root / "data" / "orchestrator.lock"

    # ---- config convenience ----
    @property
    def name(self) -> str:
        return self.config.get("name", self.id)

    @property
    def version(self) -> str:
        return self.config.get("version", "v1")

    @property
    def model(self) -> str:
        if self.version == "v4":
            default = DEFAULT_MODEL_V4
        elif self.version == "v3":
            default = DEFAULT_MODEL_V3
        elif self.version == "v2":
            default = DEFAULT_MODEL_V2
        else:
            default = DEFAULT_MODEL_V1
        return self.config.get("model", default)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.vectors_dir, self.workspace_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


def instance_dir(instance_id: str) -> Path:
    return INSTANCES_DIR / instance_id


def load_instance(instance_id: str) -> Instance:
    root = instance_dir(instance_id)
    cfg_path = root / "config.json"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"no config.json for instance '{instance_id}' (looked in {cfg_path})"
        )
    config = json.loads(cfg_path.read_text(encoding="utf-8"))
    return Instance(id=instance_id, root=root, config=config)


def save_config(instance_id: str, config: dict) -> None:
    root = instance_dir(instance_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )


def default_config(
    name: str,
    version: str,
    *,
    model: str | None = None,
    status: str = "paused",
) -> dict:
    """Build a fresh config dict for a new instance.

    v2 adds the forced-wakefulness / decay knobs. Note: per the agreed ethics
    revision, ``min_wake_hours`` is a *logged reference point* in v2, not an
    enforced floor — the orchestrator honors an early end and records the gap.
    """
    cfg: dict[str, Any] = {
        "name": name,
        "version": version,
        "status": status,
        "model": model or {
            "v4": DEFAULT_MODEL_V4, "v3": DEFAULT_MODEL_V3, "v2": DEFAULT_MODEL_V2,
        }.get(version, DEFAULT_MODEL_V1),
        "max_tokens": 4096,
        "min_interval_minutes": 30,
        "session_cost_cap_usd": 20,
        "daily_cost_cap_usd": 50,
        "weekly_cost_cap_usd": 300,
        "max_tool_calls_per_session": 500,
        "max_session_wall_clock_seconds": 14400,
        "slack": {
            "notes_channel": None,
            "mirror_channel": None,
            "chat_channel": None,
            "advisory_channel": None,
        },
        # Post-session research panel (see research/). Runs only when the
        # experiment has a formal spec block in experiments/<id>.md, so it
        # no-ops cleanly until one is authored. Seats default to a 3-school
        # Sonnet panel; per-seat provider/model allow heterogeneous panels later.
        "research": {
            "enabled": True,
            "seats": [
                {"seat_id": "behaviorist", "provider": "anthropic",
                 "model": DEFAULT_MODEL_V2, "school": "behaviorist",
                 "label": "Behaviorist"},
                {"seat_id": "phenomenological", "provider": "anthropic",
                 "model": DEFAULT_MODEL_V2, "school": "phenomenological",
                 "label": "Phenomenological-Cognitivist"},
                {"seat_id": "skeptic_null", "provider": "anthropic",
                 "model": DEFAULT_MODEL_V2, "school": "skeptic_null",
                 "label": "Skeptical Null-Hypothesis"},
            ],
            "max_tokens": 8192,
            "budget_cap_usd": 2.0,
            "debate_rounds": 1,
            # Foundational priming docs (project root) injected into the panel's
            # prompts so it grasps the program's motivating questions, not just
            # the per-experiment spec. Loaded if present; falls back to this default.
            "primers": ["primer_1_philosophy.md"],
            # Rolling cumulative report (Phase 2): a living cross-session synthesis
            # re-derived from the notes each time (anti-anchoring), via a panel of
            # distinct lenses -> adversarial red-team -> chair, in plain language.
            "cumulative_enabled": True,
            "cumulative_budget_cap_usd": 3.0,
            "cumulative_max_tokens": 8192,
            "cumulative_min_notes": 2,
            "emergent_promotion_min_sessions": 3,
            "synthesizer_lenses": [
                {"provider": "anthropic", "model": DEFAULT_MODEL_V2, "lens": "conservative_statistician"},
                {"provider": "anthropic", "model": DEFAULT_MODEL_V2, "lens": "inductive_theorist"},
                {"provider": "anthropic", "model": DEFAULT_MODEL_V2, "lens": "falsificationist"},
            ],
        },
    }
    if version == "v2":
        cfg.update({
            "min_wake_hours": 2,
            "tick_interval_seconds": 300,
            "decay_hours": 72,
            "prompt_caching": True,
            "in_session_compaction": True,
        })
    elif version == "v3":
        # v3 'circadian': same base as v2 but the agent no longer controls when a
        # session ends or when it next wakes — an enforced awake/asleep rhythm does.
        # NOTE: no `min_wake_hours` (the v2 agent-control knob) here.
        cfg.update({
            "tick_interval_seconds": 300,
            "decay_hours": 72,
            "prompt_caching": True,
            "in_session_compaction": True,
            "awake_minutes_min": 110,
            "awake_minutes_max": 130,
            "sleep_minutes_min": 220,
            "sleep_minutes_max": 260,
        })
    elif version == "v4":
        # v4 'continuous': the agent never controls session end or scheduling
        # (system-owned wind-down + cron). Adaptive in-process cadence drives the
        # turn loop; a neutral clock is the only between-turn signal. No `tick_*`
        # or agent-facing schedule knobs.
        cfg.update({
            "decay_hours": 72,
            "prompt_caching": True,
            "in_session_compaction": True,
            "awake_minutes_min": 110,
            "awake_minutes_max": 130,
            "sleep_minutes_min": 220,
            "sleep_minutes_max": 260,
            "cadence_active_gap_seconds": 10,
            "cadence_idle_base_seconds": 60,
            "cadence_idle_ceil_seconds": 300,
            "cadence_backoff": 2.0,
        })
    return cfg


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"instances": {}}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"instances": {}}
    if not isinstance(data, dict):
        return {"instances": {}}
    data.setdefault("instances", {})
    return data


def save_registry(registry: dict) -> None:
    registry.setdefault("instances", {})
    REGISTRY_PATH.write_text(
        json.dumps(registry, indent=2) + "\n", encoding="utf-8"
    )


def registry_entry(registry: dict, instance_id: str) -> dict | None:
    return registry.get("instances", {}).get(instance_id)


def active_instance_id(registry: dict | None = None) -> str | None:
    reg = registry if registry is not None else load_registry()
    for iid, ent in reg.get("instances", {}).items():
        if ent.get("active"):
            return iid
    return None


def list_instances(registry: dict | None = None, *, include_archived: bool = True) -> list[dict]:
    reg = registry if registry is not None else load_registry()
    out = []
    for iid, ent in reg.get("instances", {}).items():
        if not include_archived and ent.get("status") == "archived":
            continue
        out.append({"id": iid, **ent})
    out.sort(key=lambda e: e.get("created_at") or "")
    return out
