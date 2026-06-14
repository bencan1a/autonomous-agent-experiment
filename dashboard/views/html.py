"""HTML page routes for the dashboard (Blueprint 'html').

Route bodies moved verbatim from dashboard/app.py (P1-3). The only mechanical
edits are @app.route -> @bp.route. These render templates over the read-only,
g.instance-scoped data assembled in dashboard.data.
"""

from __future__ import annotations

from datetime import datetime

from flask import (
    Blueprint,
    abort,
    g,
    render_template,
    request,
    send_from_directory,
)

from research.store import ResearchStore
from system_prompt import SYSTEM_PROMPT
from agent_tools.registry import TOOLS_SPEC

from dashboard.data import (
    ROOT,
    UTC,
    _claude_md_path,
    _dashboard_context,
    _duration_str,
    _read_text_or_none,
    _resolve_workspace_path,
    _session_live,
    _status,
    _store,
    _workspace_root,
    _workspace_tree,
)

bp = Blueprint("html", __name__)


@bp.route("/")
def index():
    return render_template("index.html", **_dashboard_context())


@bp.route("/logs")
def logs():
    return render_template("logs.html", **_dashboard_context())


@bp.route("/research")
def research_view():
    store = _store()
    rs = ResearchStore(g.instance.episodes_db)
    notes = rs.recent_notes(n=200)
    # Attach the per-session Fleiss kappa to each note for the list view.
    for n in notes:
        kap = rs.kappa_for_session(n["session_id"])
        n["fleiss"] = next((k["value"] for k in kap if k.get("metric") == "fleiss"), None)

    # The operationalized coding scheme + approval status for this experiment.
    from research.spec import load_spec

    spec = load_spec(ROOT, g.instance.id)
    prereg = None
    if spec is not None:
        prereg = rs.get_prereg(g.instance.id, spec.spec_hash) or rs.latest_prereg(g.instance.id)
    else:
        prereg = rs.latest_prereg(g.instance.id)

    cumulative = rs.latest_cumulative(g.instance.id)

    return render_template(
        "research.html",
        status=_status(),
        notes=notes,
        spec=spec,
        prereg=prereg,
        cumulative=cumulative,
        now=datetime.now(UTC).isoformat(),
    )


@bp.route("/session/<int:session_id>")
def session_detail(session_id: int):
    store = _store()
    # Look up the session row directly from the full list (small N).
    all_sessions = store.all_sessions()
    session = next((s for s in all_sessions if s["id"] == session_id), None)
    if not session:
        abort(404)

    actions = store.actions_for_session(session_id)
    subagent_calls = [
        c for c in store.recent_subagent_calls(n=100000) if c["session_id"] == session_id
    ]
    subagent_calls.sort(key=lambda c: c["id"])

    # Find the linked episode (if any). Episodes carry `session_id` after the
    # ALTER migration. Empty DBs / older rows may not.
    episodes = store.all_episodes()
    linked_episode = next(
        (e for e in episodes if e.get("session_id") == session_id), None
    )

    live = _session_live(session)
    session_cost = store.session_cost(session_id)
    # Wall-clock duration (live = now)
    duration = _duration_str(session.get("started_at"), session.get("ended_at"))

    # Research panel artifacts (if a panel reviewed this session).
    research_note = None
    research_seats: list = []
    research_kappa: list = []
    try:
        rs = ResearchStore(g.instance.episodes_db)
        research_note = rs.note_for_session(session_id)
        research_seats = rs.seat_notes_for_session(session_id)
        research_kappa = rs.kappa_for_session(session_id)
    except Exception:
        pass

    return render_template(
        "session.html",
        status=_status(),
        session=session,
        actions=actions,
        subagent_calls=subagent_calls,
        linked_episode=linked_episode,
        is_live=live,
        session_cost=round(session_cost, 4),
        duration=duration,
        research_note=research_note,
        research_seats=research_seats,
        research_kappa=research_kappa,
        now=datetime.now(UTC).isoformat(),
    )


@bp.route("/actions")
def actions_log():
    store = _store()
    try:
        limit = int(request.args.get("limit", "200"))
    except ValueError:
        limit = 200
    limit = max(1, min(limit, 1000))
    actions = store.recent_actions(limit=limit)
    return render_template(
        "actions.html",
        status=_status(),
        actions=actions,
        limit=limit,
        now=datetime.now(UTC).isoformat(),
    )


@bp.route("/claude_md")
def claude_md_view():
    store = _store()
    claude_md_path = _claude_md_path()
    claude_md_current: str | None = None
    claude_md_size: int | None = None
    try:
        if claude_md_path.exists():
            claude_md_current = claude_md_path.read_text(encoding="utf-8", errors="replace")
            claude_md_size = len(claude_md_current.encode("utf-8"))
    except OSError:
        claude_md_current = None

    history = store.claude_md_history(limit=50)
    return render_template(
        "claude_md.html",
        status=_status(),
        claude_md_current=claude_md_current,
        claude_md_size=claude_md_size,
        claude_md_path=str(claude_md_path),
        history=history,
        now=datetime.now(UTC).isoformat(),
    )


@bp.route("/subagents")
def subagents_log():
    store = _store()
    calls = store.recent_subagent_calls(n=1000)
    return render_template(
        "subagents.html",
        status=_status(),
        calls=calls,
        now=datetime.now(UTC).isoformat(),
    )


@bp.route("/workspace")
def workspace_index():
    entries = _workspace_tree()
    total_files = sum(1 for e in entries if not e["is_dir"])
    total_bytes = sum(e["size"] for e in entries if not e["is_dir"])
    return render_template(
        "workspace.html",
        entries=entries,
        total_files=total_files,
        total_bytes=total_bytes,
        workspace_root=str(_workspace_root()),
        now=datetime.now(UTC).isoformat(),
    )


@bp.route("/workspace/view/<path:relpath>")
def workspace_view(relpath: str):
    resolved = _resolve_workspace_path(relpath)
    if resolved is None or not resolved.exists():
        abort(404)
    if resolved.is_dir():
        # Showing a directory view inline is overkill — bounce to /workspace.
        abort(404)
    text, truncated, size = _read_text_or_none(resolved)
    return render_template(
        "workspace_view.html",
        relpath=relpath,
        name=resolved.name,
        size=size,
        truncated=truncated,
        text=text,
        is_binary=(text is None and size > 0),
        mtime=datetime.fromtimestamp(resolved.stat().st_mtime, UTC).isoformat(),
        now=datetime.now(UTC).isoformat(),
    )


@bp.route("/workspace/raw/<path:relpath>")
def workspace_raw(relpath: str):
    resolved = _resolve_workspace_path(relpath)
    if resolved is None or not resolved.exists() or resolved.is_dir():
        abort(404)
    # send_from_directory rejects path-traversal itself; we've also pre-checked.
    return send_from_directory(
        _workspace_root().resolve(),
        relpath,
        as_attachment=False,
    )


@bp.route("/prompt")
def prompt_view():
    """Show what the agent sees: system prompt + tools + per-invocation scaffolding."""
    # The "lifecycle note" appended to every user prompt — mirror from
    # context_assembly.py so it's visible without reading source.
    lifecycle_note = (
        "You are running as a long-lived session that started on cron wake-up. "
        "You have tools available — call them as needed in any sequence. "
        "When you're done thinking for now, call the finish_session tool with your "
        "final state (current_focus, internal_state, decisions_made, journal_entry, "
        "next_invoke_minutes). The process exits when finish_session is called. "
        "Minimum next_invoke_minutes is 30 (lower values will be clamped). "
        "If next_invoke_minutes is null, no further cron entry is installed and "
        "you will not be woken again unless Ben manually restarts you."
    )
    context_blocks = [
        ("Your AGENTS.md", "Notes the agent has written to itself (current file from the workspace; legacy CLAUDE.md read via fallback). Loaded only when non-empty."),
        ("Clock + invocation #", "Current datetime (UTC), invocation number, days since start."),
        ("Last episodes", "Up to 3 most recent episodes in full (focus, actions, internal_state, journal, decisions, next_invoke_minutes)."),
        ("Semantic recall", "Top-k semantic search hits over older episodes + weekly summaries, queried by the most recent episode's current_focus. Skipped if no focus yet."),
        ("Pending capability requests", "Tier-2 requests the agent has made that haven't been answered yet, with status."),
        ("Messages from Ben since last invocation", "DMs Ben sent since the last successfully-logged episode. Cursor advances only after the episode is logged so nothing is lost on a crash."),
        ("Lifecycle note (always appended)", lifecycle_note),
    ]
    return render_template(
        "prompt.html",
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS_SPEC,
        context_blocks=context_blocks,
        model=(g.instance.config.get("model") if getattr(g, "instance", None) else None) or "(unset)",
        now=datetime.now(UTC).isoformat(),
    )
