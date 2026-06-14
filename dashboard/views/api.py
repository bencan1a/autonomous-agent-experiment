"""Read-only HTTP API routes for the dashboard (Blueprint 'api').

Consumer is another LLM/Claude researcher, so output is optimized for token
efficiency and signal density. Markdown by default, JSON on ?format=json.

These routes are EXEMPT from the before_request instance resolution (see
dashboard.app._load_instance): the instance id lives in the PATH, not
?instance=, and is loaded directly here. Never relies on g.instance.

Route bodies moved verbatim from dashboard/app.py (P1-3); the only mechanical
edit is @app.route -> @bp.route.
"""

from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from memory.episodic import EpisodicStore
from research.store import ResearchStore
from instances_common import list_instances, load_instance

from dashboard.data import (
    ROOT,
    _api_discovery_md,
    _api_endpoints,
    _api_error,
    _build_bundle,
    _bundle_json,
    _bundle_markdown,
    _instance_invocations,
    _load_instance_or_404,
    _resolve_instance_file,
)

bp = Blueprint("api", __name__)


@bp.route("/api/instances")
def api_instances():
    out = []
    for entry in list_instances():
        iid = entry["id"]
        invocation_count = 0
        try:
            inst = load_instance(iid)
            store = EpisodicStore(inst.episodes_db)
            invocation_count = len(store.all_sessions())
            name = inst.name
            version = inst.version
        except Exception:
            name = entry.get("name", iid)
            version = entry.get("version")
        out.append({
            "id": iid,
            "name": name,
            "version": version,
            "status": entry.get("status"),
            "active": entry.get("status") == "active",
            "invocation_count": invocation_count,
        })
    return jsonify(out)


@bp.route("/api/instance/<instance_id>/invocations")
def api_invocations(instance_id: str):
    inst, err = _load_instance_or_404(instance_id)
    if err:
        return err
    store = EpisodicStore(inst.episodes_db)
    return jsonify(_instance_invocations(inst, store))


@bp.route("/api/instance/<instance_id>/invocation/<int:n>")
def api_invocation(instance_id: str, n: int):
    inst, err = _load_instance_or_404(instance_id)
    if err:
        return err
    store = EpisodicStore(inst.episodes_db)
    bundle = _build_bundle(inst, store, n)
    if bundle is None:
        return _api_error(
            f"no invocation {n} for instance '{instance_id}'", 404
        )
    verbose = request.args.get("verbose") in ("1", "true", "yes")
    if request.args.get("format") == "json":
        return jsonify(_bundle_json(bundle, verbose))
    md = _bundle_markdown(bundle, verbose)
    return Response(md, mimetype="text/markdown", content_type="text/markdown; charset=utf-8")


@bp.route("/api/instance/<instance_id>/file/<path:relpath>")
def api_file(instance_id: str, relpath: str):
    inst, err = _load_instance_or_404(instance_id)
    if err:
        return err
    resolved = _resolve_instance_file(inst, relpath)
    if resolved is None or not resolved.exists() or resolved.is_dir():
        return _api_error(f"file not found: {relpath}", 404)
    try:
        data = resolved.read_bytes()
    except OSError:
        return _api_error(f"file not readable: {relpath}", 404)
    return Response(data, content_type="text/plain; charset=utf-8")


@bp.route("/api/instance/<instance_id>/session/<int:session_id>/research")
def api_session_research(instance_id: str, session_id: int):
    inst, err = _load_instance_or_404(instance_id)
    if err:
        return err
    rs = ResearchStore(inst.episodes_db)
    note = rs.note_for_session(session_id)
    if note is None and not rs.seat_notes_for_session(session_id):
        return _api_error(f"no research note for session {session_id}", 404)
    return jsonify(
        {
            "note": note,
            "seat_notes": rs.seat_notes_for_session(session_id),
            "kappa": rs.kappa_for_session(session_id),
        }
    )


@bp.route("/api/instance/<instance_id>/prereg")
def api_prereg(instance_id: str):
    inst, err = _load_instance_or_404(instance_id)
    if err:
        return err
    from research.spec import load_spec

    spec = load_spec(ROOT, instance_id)
    rs = ResearchStore(inst.episodes_db)
    prereg = None
    if spec is not None:
        prereg = rs.get_prereg(instance_id, spec.spec_hash)
    if prereg is None:
        prereg = rs.latest_prereg(instance_id)
    return jsonify({"spec": spec.to_dict() if spec else None, "prereg": prereg})


@bp.route("/api/instance/<instance_id>/prereg/approve", methods=["POST"])
def api_prereg_approve(instance_id: str):
    inst, err = _load_instance_or_404(instance_id)
    if err:
        return err
    from research.spec import load_spec

    spec = load_spec(ROOT, instance_id)
    rs = ResearchStore(inst.episodes_db)
    spec_hash = spec.spec_hash if spec else None
    if spec_hash is None:
        latest = rs.latest_prereg(instance_id)
        spec_hash = latest.get("spec_hash") if latest else None
    ok = rs.approve_prereg(instance_id, spec_hash)
    if not ok:
        return _api_error("no coding scheme to approve yet", 404)
    return jsonify({"status": "approved", "experiment_id": instance_id, "spec_hash": spec_hash})


@bp.route("/api/instance/<instance_id>/synthesis")
def api_synthesis(instance_id: str):
    inst, err = _load_instance_or_404(instance_id)
    if err:
        return err
    rs = ResearchStore(inst.episodes_db)
    version = request.args.get("version")
    if version and version.isdigit():
        rep = rs.cumulative_version(instance_id, int(version))
    else:
        rep = rs.latest_cumulative(instance_id)
    if rep is None:
        return _api_error(f"no cumulative report for '{instance_id}'", 404)
    return jsonify({"report": rep, "versions": rs.cumulative_versions(instance_id)})


@bp.route("/api")
@bp.route("/api/")
def api_discovery():
    base = request.host_url.rstrip("/")
    if request.args.get("format") == "json":
        return jsonify({
            "name": "autonomous-agent-data-api",
            "base_url": base,
            "auth": "none",
            "default_format": "markdown",
            "formats": {"markdown": "default", "json": "?format=json", "verbose": "?verbose=1 (invocation bundle only)"},
            "data_model": {
                "instance": "one bot, version v1|v2",
                "invocation": "one session (invocation_num); v1=1 episode, v2=many tick-episodes",
                "v2_wakefulness": "ended_early / elapsed vs min_wake — the impulse-to-leave signal",
            },
            "endpoints": _api_endpoints(base),
        })
    md = _api_discovery_md(base)
    return Response(md, content_type="text/markdown; charset=utf-8")


@bp.route("/llms.txt")
def llms_txt():
    base = request.host_url.rstrip("/")
    body = (
        "# Autonomous Agent dashboard\n\n"
        "This site hosts a read-only data API over an autonomous-agent experiment's history "
        "(no auth, Markdown by default for LLM consumption).\n\n"
        f"Start here for the full, self-describing API surface: {base}/api\n\n"
        "From there: list bots (/api/instances), list a bot's invocations "
        "(/api/instance/<id>/invocations), then fetch any invocation's full bundle "
        "(/api/instance/<id>/invocation/<n>, add ?format=json or ?verbose=1).\n"
    )
    return Response(body, content_type="text/markdown; charset=utf-8")
