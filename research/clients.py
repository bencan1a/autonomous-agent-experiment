"""Shared bootstrap for the operator research scripts and CLI subcommands.

Six call sites (scripts/run_research_panel.py, run_cumulative_synthesis.py,
run_advisory_watch.py, review_proposal.py, and two instance_manager.py
subcommands) all repeated the same opening ritual:

    load_dotenv(.env, override=True)        # the documented secret gotcha
    anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    EpisodicStore(inst.episodes_db) / ResearchStore(inst.episodes_db)
    optional SemanticStore(inst.vectors_dir)

The ``override=True`` matters: the shell exports ``ANTHROPIC_API_KEY=""`` so the
key MUST come from ``.env`` and overwrite the empty shell value (the same thing
the orchestrator does). Keeping that in exactly one place is the point of this
module — see CLAUDE.md's secret-loading gotcha.

This module is intentionally light: it imports ``anthropic`` and the stores only
when ``research_clients`` (or the small helpers) are called, so importing it does
not drag the Anthropic SDK / embedding model into unrelated subcommands.
instance_manager keeps its lazy-import-inside-command pattern by importing THIS
helper inside the command bodies.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from instances_common import AGENT_ROOT, SHARED_HF_CACHE, Instance, load_instance

if TYPE_CHECKING:  # avoid importing the heavy modules at import time
    from memory.episodic import EpisodicStore
    from memory.semantic import SemanticStore
    from research.store import ResearchStore


@dataclass
class ResearchClients:
    """The bootstrapped stores + Anthropic client for one instance.

    Fields:
      - ``instance``  : the resolved :class:`instances_common.Instance`.
      - ``episodic``  : an :class:`EpisodicStore` over ``instance.episodes_db``.
      - ``research``  : a :class:`ResearchStore` over the same db file.
      - ``anthropic`` : a configured ``anthropic.Anthropic`` client.
      - ``semantic``  : a :class:`SemanticStore` over ``instance.vectors_dir``
                        when ``embed=True``, else ``None``.
      - ``agent_root``: the repo root (``AGENT_ROOT``), for callers that pass it
                        on to research/spec.py / panel runners.
    """

    instance: Instance
    episodic: "EpisodicStore"
    research: "ResearchStore"
    anthropic: Any
    semantic: "SemanticStore | None"
    agent_root: Any  # pathlib.Path

    def as_panel_kwargs(self) -> dict[str, Any]:
        """Keyword block shared by the research-panel / synthesis runners."""
        return {
            "instance": self.instance,
            "episodic": self.episodic,
            "research_store": self.research,
            "anthropic_client": self.anthropic,
            "agent_root": self.agent_root,
            "semantic": self.semantic,
        }


def load_env(*, override: bool = True) -> None:
    """Load the shared ``.env``, overriding the (empty) shell secret values.

    Best-effort: if python-dotenv or the file is missing, the caller will fail
    later with a clear ``KeyError`` on the missing key. Kept here so the
    ``override=True`` secret-loading gotcha lives in exactly one place.
    """
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    env_path = AGENT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=override)


def anthropic_client() -> Any:
    """Return a configured ``anthropic.Anthropic`` client.

    Assumes :func:`load_env` has already populated ``ANTHROPIC_API_KEY``.
    """
    import anthropic

    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def research_clients(instance_id: str, *, embed: bool = True) -> ResearchClients:
    """Bootstrap the stores + Anthropic client for ``instance_id``.

    Loads ``.env`` with ``override=True`` (the documented secret gotcha), points
    ``HF_HOME`` at the shared embedding cache, resolves the instance, and opens an
    :class:`EpisodicStore` + :class:`ResearchStore` over its db plus an Anthropic
    client. When ``embed`` is true a :class:`SemanticStore` is opened over the
    instance's vectors dir (this triggers a lazy embedding-model load); pass
    ``embed=False`` to skip it (faster, no model load).

    Raises whatever :func:`load_instance` raises on an unknown id (callers that
    want a friendly error should catch it and print to stderr).
    """
    load_env(override=True)
    os.environ.setdefault("HF_HOME", str(SHARED_HF_CACHE))

    inst = load_instance(instance_id)

    from memory.episodic import EpisodicStore
    from research.store import ResearchStore

    episodic = EpisodicStore(inst.episodes_db)
    research = ResearchStore(inst.episodes_db)

    semantic = None
    if embed:
        from memory.semantic import SemanticStore

        semantic = SemanticStore(inst.vectors_dir)

    return ResearchClients(
        instance=inst,
        episodic=episodic,
        research=research,
        anthropic=anthropic_client(),
        semantic=semantic,
        agent_root=AGENT_ROOT,
    )
