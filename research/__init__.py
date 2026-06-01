"""Research-assistant panel: post-session scientific review of subject agents.

Operator/researcher infrastructure (like ``maintenance.py``), deliberately kept
OUT of ``agent_tools/`` so nothing here ever enters ``TOOLS_SPEC`` or a subject
agent's context. A panel of researcher "seats" reviews each completed session
against the experiment's human-authored formal spec and writes a per-session
research note, surfaced on the dashboard.

The entrypoint is ``research.panel.run_research_panel`` — called inline at the
end of every session runner, wrapped so it can never block the next wake.
"""

from __future__ import annotations

__all__ = ["run_research_panel"]


def run_research_panel(*args, **kwargs):  # pragma: no cover - thin re-export
    # Imported lazily so importing the package never drags in heavy deps.
    from research.panel import run_research_panel as _impl

    return _impl(*args, **kwargs)
