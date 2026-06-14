"""Flask dashboard on :8081 — SSH-tunnel access only.

Read-only view of the agent's history. Never mutates state.

This module is the app entry point + request lifecycle only. The data-assembly
and serialization layer lives in dashboard/data.py; the routes live in the three
blueprints under dashboard/views/ (html, control, api). See P1-3 in CODE_REVIEW.md.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    g,
    render_template,
    request,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=True)

from memory.episodic import EpisodicStore  # noqa: E402
from instances_common import (  # noqa: E402
    active_instance_ids,
    CANDIDATE_MODELS,
    list_instances,
    load_instance,
)

from dashboard.data import (  # noqa: E402
    UTC,
    _duration_str,
    _relative_time,
    _tool_category,
)
from dashboard.views.html import bp as html_bp  # noqa: E402
from dashboard.views.control import bp as control_bp  # noqa: E402
from dashboard.views.api import bp as api_bp  # noqa: E402


app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
# Re-read templates from disk when they change, so edits to dashboard/templates/*.html
# take effect on the next request without restarting the long-lived process. Cheap
# (an mtime stat per render); independent of debug mode, which stays off in prod.
app.config["TEMPLATES_AUTO_RELOAD"] = True


def _resolve_instance():
    """Resolve the selected instance for this request.

    Preference order:
      1. ?instance=<id> if it names a loadable instance
      2. the most-recently-woken active instance (fork branches can co-run, so
         there may be several active; pick deterministically by last_wake)
      3. the first instance from list_instances()
    Returns an Instance or None (zero instances / all unloadable).
    """
    instances = list_instances()
    valid_ids = {i["id"] for i in instances}

    requested = request.args.get("instance")
    candidates: list[str] = []
    if requested and requested in valid_ids:
        candidates.append(requested)
    candidates.extend(iid for iid in active_instance_ids() if iid in valid_ids)

    for iid in candidates:
        try:
            return load_instance(iid)
        except Exception:
            continue
    return None


@app.before_request
def _load_instance():
    # The /api/ routes carry the instance id in their PATH (not ?instance=) and
    # load it themselves. Skip query-based resolution and the no_instances
    # short-circuit, which would otherwise hijack every /api/ request.
    if request.path == "/api" or request.path.startswith("/api/") or request.path == "/llms.txt":
        return
    g.instance = _resolve_instance()
    if g.instance is None and request.endpoint != "static":
        # Zero instances configured: render a friendly empty state instead of
        # letting downstream routes crash on a missing DB / workspace.
        return render_template("no_instances.html", now=datetime.now(UTC).isoformat())


@app.after_request
def _no_cache(resp):
    # Live monitoring view: never let a browser serve a stale instance page,
    # so switching the instance dropdown always reflects fresh, scoped data.
    resp.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.context_processor
def _inject_instance_context():
    instances = list_instances()
    selected = None
    cost_total = 0.0
    current_model = None
    inst = getattr(g, "instance", None)
    if inst is not None:
        current_model = inst.model
        # Find the registry entry for richer status/active fields.
        entry = next((i for i in instances if i["id"] == inst.id), None)
        branch_label = (entry or {}).get("branch_label") or inst.config.get("branch_label")
        selected = {
            "id": inst.id,
            "name": inst.name,
            "version": inst.version,
            "status": (entry or {}).get("status"),
            "active": (entry or {}).get("status") == "active",
            "model": current_model,
            # lineage (None for non-forked instances)
            "branch_label": branch_label,
            "parent_id": (entry or {}).get("parent_id") or inst.config.get("parent_id"),
            "fork_group": (entry or {}).get("fork_group") or inst.config.get("fork_group"),
            "cycle_label": None,
        }
        try:
            store = EpisodicStore(inst.episodes_db)
            cost_total = sum(float(r["cost_usd"]) for r in store.cost_by_day())
            # cycle label e.g. "8a": the cycle this branch is on (its next
            # invocation number) + branch letter — matches the fork naming, and
            # advances 8a -> 9a -> ... as the branch runs.
            if branch_label:
                selected["cycle_label"] = f"{store.next_invocation_num()}{branch_label}"
        except Exception:
            cost_total = 0.0

    # Model choices for the "new instance" panel: the curated shortlist, with the
    # current instance's model prepended if it isn't already in the list (so the
    # panel can preselect it as the default).
    model_choices = list(CANDIDATE_MODELS)
    if current_model and current_model not in {m for m, _ in model_choices}:
        model_choices = [(current_model, f"{current_model} (current)")] + model_choices

    return {
        "instances": instances,
        "selected_instance": selected,
        "instance_cost_total": round(cost_total, 4),
        "candidate_models": model_choices,
        "current_model": current_model,
    }


# ---------- template filters ----------


@app.template_filter("fmt_dt")
def fmt_dt(s):
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return s


@app.template_filter("fmt_json")
def fmt_json(value):
    """Pretty-print JSON. Accepts dict/list or a JSON string."""
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return value
        return json.dumps(value, indent=2, default=str)
    except Exception:
        return str(value)


@app.template_filter("relative_time")
def relative_time(s):
    return _relative_time(s)


@app.template_filter("truncate_chars")
def truncate_chars(s, n=80):
    if s is None:
        return ""
    s = str(s)
    if len(s) <= n:
        return s
    return s[:n] + "…"


@app.template_filter("tool_category")
def tool_category(tool_name):
    return _tool_category(tool_name)


@app.template_filter("duration")
def duration_filter(seconds):
    """Format a duration in ms as a short human string."""
    if seconds is None:
        return "—"
    try:
        ms = int(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if ms < 1000:
        return f"{ms}ms"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f}s"
    m = int(s // 60)
    rem = int(s % 60)
    return f"{m}m {rem}s"


@app.template_filter("human_seconds")
def human_seconds_filter(seconds):
    """Format a duration in seconds as a short human string like '37m' or '1h 4m'."""
    if seconds is None:
        return "—"
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return str(seconds)
    if total < 0:
        total = 0
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem = minutes % 60
    return f"{hours}h {rem}m" if rem else f"{hours}h"


@app.template_filter("session_duration")
def session_duration_filter(session):
    """Wall-clock duration for a session dict (uses now() if still running)."""
    if not session:
        return "—"
    return _duration_str(session.get("started_at"), session.get("ended_at"))


@app.template_filter("fmt_bytes")
def fmt_bytes(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "—"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    return f"{n/1024/1024:.1f} MB"


# ---------- blueprint registration ----------

app.register_blueprint(html_bp)
app.register_blueprint(control_bp)
app.register_blueprint(api_bp)


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", "8081"))
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=False)
