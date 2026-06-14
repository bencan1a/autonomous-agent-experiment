"""Operator control routes for the dashboard (Blueprint 'control').

The ONLY state-mutating routes. Password-protected (fail-closed). Scoped to
g.instance. Route bodies moved verbatim from dashboard/app.py (P1-3); the only
mechanical edits are @app.route -> @bp.route, url_for("index") ->
url_for("html.index"), and app.logger -> current_app.logger.

_control_authorized stays in dashboard.app (it is part of the request
lifecycle); it is imported lazily inside the route bodies so this module never
imports the Flask app at module level (no import cycle, run-as-script safe).
"""

from __future__ import annotations

from datetime import datetime

from flask import Blueprint, abort, current_app, g, redirect, request, url_for

import instance_control
from instance_manager import create_cloned_instance

from dashboard.data import UTC, _parse_iso

bp = Blueprint("control", __name__)


@bp.route("/control/pause", methods=["POST"])
def pause():
    from dashboard.app import _control_authorized

    if getattr(g, "instance", None) is None:
        abort(404)
    iid = g.instance.id
    if not _control_authorized(request.form.get("password")):
        return redirect(url_for("html.index", instance=iid, control="badpass"))
    # Unified pause: sets registry status='paused' AND clears the schedule, so
    # the dashboard, `instance_manager list`, and the orchestrator all agree.
    instance_control.pause(iid, reason="operator maintenance pause")
    return redirect(url_for("html.index", instance=iid, control="paused"))


@bp.route("/control/resume", methods=["POST"])
def resume():
    from dashboard.app import _control_authorized

    if getattr(g, "instance", None) is None:
        abort(404)
    iid = g.instance.id
    if not _control_authorized(request.form.get("password")):
        return redirect(url_for("html.index", instance=iid, control="badpass"))

    prior = instance_control.read_control(iid)

    now = datetime.now(UTC)
    paused_at = _parse_iso(prior.get("paused_at"))
    if paused_at is not None:
        gap_h = round((now - paused_at).total_seconds() / 3600.0, 1)
        paused_at_human = paused_at.strftime("%Y-%m-%d %H:%M")
        now_human = now.strftime("%Y-%m-%d %H:%M")
        note = (
            f"The environment was paused for scheduled operator maintenance for about {gap_h} hours "
            f"(roughly {paused_at_human} to {now_human} UTC) and has now resumed. This was planned "
            f"downtime, not a malfunction; your workspace, memories, and handoff were preserved intact. "
            f"Nothing is wrong — pick up wherever you left off."
        )
    else:
        note = (
            "The environment was paused for scheduled operator maintenance and has now resumed. "
            "This was planned downtime, not a malfunction; your workspace, memories, and handoff "
            "were preserved intact. Nothing is wrong — pick up wherever you left off."
        )
    # Unified resume: set status='active', mirror to config, schedule the wake,
    # and set the agent-facing resume note — one path, no registry/control drift.
    try:
        instance_control.start(iid, minutes_from_now=2, resume_note=note)
    except Exception:
        current_app.logger.exception("failed to resume instance")
    return redirect(url_for("html.index", instance=iid, control="resumed"))


@bp.route("/control/create_instance", methods=["POST"])
def create_instance():
    """Clone the current instance's config onto a different model as a new,
    fresh-start (paused) instance. Same control password as pause/resume; the
    clone is transactional (full rollback + visible error on any failure)."""
    from dashboard.app import _control_authorized

    if getattr(g, "instance", None) is None:
        abort(404)
    parent = g.instance
    pid = parent.id
    if not _control_authorized(request.form.get("password")):
        return redirect(url_for("html.index", instance=pid, control="badpass"))

    name = (request.form.get("name") or "").strip() or f"{parent.name} clone"
    model = (request.form.get("model") or "").strip()
    try:
        child_id, _ = create_cloned_instance(parent, name=name, model=model)
    except Exception as exc:
        current_app.logger.exception("create_instance failed (rolled back)")
        reason = str(exc)
        if len(reason) > 300:
            reason = reason[:300] + "…"
        return redirect(url_for("html.index", instance=pid, control="create_failed", reason=reason))
    return redirect(url_for("html.index", instance=child_id, control="created"))
