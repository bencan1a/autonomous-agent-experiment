"""Behavior-level tests for the dashboard (Flask test_client). Plain runner — NO pytest.

These lock in the dashboard's *observable* contract so the blueprint/data-layer
refactor (P1-3) can land safely: assertions go through HTTP (test_client),
``url_for``, and response bodies — never internal function locations — so the
suite survives moving routes into blueprints and helpers into a data module.

Fully offline: a temp ``instances/`` tree + a seeded SQLite episodes db. No real
crontab is touched — only the *unauthorized* control path is exercised (the
authorized pause/resume flow lives in test_state_model and would hit cron).

    ./venv/bin/python tests/test_dashboard.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instances_common as ic  # noqa: E402
import instance_manager as im  # noqa: E402
import instance_control as ictrl  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402

import dashboard.app as dash  # noqa: E402
from flask import url_for  # noqa: E402

CONTROL_PW = "test-control-pw"


# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #

@contextmanager
def dash_env(*, seed: bool = True, version: str = "v2"):
    """Redirect instance path constants to a temp tree; optionally seed one
    realistic instance with a session/episode/action/workspace-file."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        saved = {
            (ic, "INSTANCES_DIR"): ic.INSTANCES_DIR,
            (ic, "REGISTRY_PATH"): ic.REGISTRY_PATH,
            (ic, "REGISTRY_LOCK"): ic.REGISTRY_LOCK,
            (im, "AGENT_ROOT"): im.AGENT_ROOT,
        }
        ic.INSTANCES_DIR = tmp / "instances"
        ic.REGISTRY_PATH = tmp / "registry.json"
        ic.REGISTRY_LOCK = tmp / "registry.json.lock"
        im.AGENT_ROOT = tmp
        (tmp / "instances").mkdir(parents=True, exist_ok=True)
        dash.app.config.update(TESTING=True)
        inst = _seed_instance(version) if seed else None
        try:
            yield tmp, inst
        finally:
            for (obj, attr), val in saved.items():
                setattr(obj, attr, val)


def _seed_instance(version: str):
    iid = f"{version}-dash"
    cfg = ic.default_config(iid, version)
    cfg["status"] = "active"
    inst = ic.Instance(iid, ic.instance_dir(iid), cfg)
    inst.ensure_dirs()
    ic.save_config(iid, cfg)
    with ic.registry_txn() as reg:
        reg.setdefault("instances", {})[iid] = {
            "id": iid, "name": cfg["name"], "version": version,
            "status": "active", "created_at": ic.now_iso(),
            "active": True, "last_wake": ic.now_iso(),
        }
    inst = ic.load_instance(iid)

    # a workspace file (exercises workspace_view/raw + the /api file endpoint)
    (inst.workspace_dir / "AGENTS.md").write_text(
        "# Notes\nremember to test the dashboard.\n", encoding="utf-8"
    )

    # episodic data: one finished session with an episode + action + v2 record
    store = EpisodicStore(inst.episodes_db)
    n = store.next_invocation_num()
    sid = store.start_session(n, pid=4321)
    store.log_action(sid, "web_search", json.dumps({"query": "x"}), "ok 3 results", None, 120)
    store.log_episode(
        invocation_num=n, current_focus="testing the dashboard",
        actions_taken=["web_search (1)"], decisions_made="seed it",
        internal_state="curious", journal_entry="hello world",
        next_invoke_minutes=30, raw_output="raw model output",
        tokens_in=100, tokens_out=50, cost_usd=0.01, session_id=sid,
    )
    store.add_cost(tokens_in=100, tokens_out=50, cost_usd=0.01)
    store.end_session(sid, "completed", end_reason="end_session")
    store.update_session_stats(sid, total_tool_calls=1, total_cost_usd=0.01)
    ts = ic.now_iso()
    store.log_v2_session(
        session_id=sid, started_at=ts, ended_at=ts, num_ticks=2,
        elapsed_seconds=120.0, min_wake_seconds=7200.0, ended_early=False,
        end_reason="end_session", next_invoke_minutes=30,
        decayed_count=0, consolidated_count=0,
    )
    return inst


def _client():
    return dash.app.test_client()


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #

_HTML_ROUTES = ["/", "/logs", "/research", "/actions", "/claude_md",
                "/subagents", "/workspace", "/prompt"]


def scenario_html_routes():
    """Every HTML page renders 200 with a seeded instance (exercises templates +
    url_for + the data-assembly layer)."""
    with dash_env() as (_tmp, _inst):
        c = _client()
        for path in _HTML_ROUTES:
            r = c.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
        for path in ["/session/1", "/workspace/view/AGENTS.md", "/workspace/raw/AGENTS.md"]:
            r = c.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
    return f"{len(_HTML_ROUTES) + 3} HTML routes return 200"


def scenario_no_instances():
    """Zero instances → friendly empty state, never a 500."""
    with dash_env(seed=False) as (_tmp, _inst):
        c = _client()
        r = c.get("/")
        assert r.status_code == 200, r.status_code
        assert "No instances" in r.get_data(as_text=True), "expected empty-state marker"
        for path in ["/logs", "/actions", "/research", "/prompt"]:
            assert c.get(path).status_code == 200, f"{path} 500'd on empty state"
    return "zero-instance empty state renders (no 500s)"


# The 13 endpoints templates reference via url_for. This guard fails loudly if a
# refactor renames an endpoint without updating it everywhere.
_TEMPLATE_ENDPOINTS = [
    ("html.index", {}), ("html.logs", {}), ("html.research_view", {}),
    ("html.actions_log", {}), ("html.claude_md_view", {}),
    ("html.subagents_log", {}), ("html.workspace_index", {}),
    ("html.prompt_view", {}), ("control.resume", {}), ("control.create_instance", {}),
    ("html.session_detail", {"session_id": 1}),
    ("html.workspace_view", {"relpath": "AGENTS.md"}),
    ("html.workspace_raw", {"relpath": "AGENTS.md"}),
]


def scenario_url_for_stable():
    """url_for resolves for every endpoint the templates name."""
    with dash_env() as (_tmp, _inst):
        with dash.app.test_request_context("/"):
            for ep, kw in _TEMPLATE_ENDPOINTS:
                assert url_for(ep, **kw), ep
    return f"url_for resolves for all {len(_TEMPLATE_ENDPOINTS)} template endpoints"


def scenario_api():
    """The /api/* + /llms.txt surface: exempt from instance resolution, loads its
    own instance from the path, and serves JSON/Markdown as documented."""
    with dash_env() as (_tmp, inst):
        c = _client()
        iid = inst.id

        # discovery + llms.txt (exempt from the before_request resolver)
        for path in ["/api", "/api/", "/llms.txt"]:
            assert c.get(path).status_code == 200, f"{path} not 200"

        r = c.get("/api/instances")
        assert r.status_code == 200 and iid in r.get_data(as_text=True), "instance missing from /api/instances"

        r = c.get(f"/api/instance/{iid}/invocations")
        assert r.status_code == 200, r.status_code
        assert isinstance(r.get_json(), list) and r.get_json(), "expected >=1 invocation"

        # invocation bundle — markdown (default) + json + verbose
        rmd = c.get(f"/api/instance/{iid}/invocation/1")
        assert rmd.status_code == 200 and "markdown" in rmd.content_type, rmd.content_type
        assert c.get(f"/api/instance/{iid}/invocation/1?format=json").get_json() is not None
        assert c.get(f"/api/instance/{iid}/invocation/1?format=json&verbose=1").status_code == 200

        # workspace file via the api
        rf = c.get(f"/api/instance/{iid}/file/AGENTS.md")
        assert rf.status_code == 200 and "remember to test" in rf.get_data(as_text=True)

        # 404s: unknown instance + nonexistent invocation, as JSON errors
        assert c.get("/api/instance/nope/invocations").status_code == 404
        assert c.get(f"/api/instance/{iid}/invocation/999").status_code == 404
        assert "error" in c.get("/api/instance/nope/invocations").get_json()

        # prereg / synthesis / session-research: none seeded → 404, never 500
        for path in [f"/api/instance/{iid}/prereg",
                     f"/api/instance/{iid}/synthesis",
                     f"/api/instance/{iid}/session/1/research"]:
            assert c.get(path).status_code in (200, 404), f"{path} 500'd"
    return "api discovery/instances/invocation/file + 404s behave"


def scenario_control_fail_closed():
    """Control routes are fail-closed: no/var bad password → redirect to a
    badpass notice and NO state change. (Authorized path hits cron; covered in
    test_state_model.)"""
    with dash_env() as (_tmp, inst):
        c = _client()
        iid = inst.id
        for path in ["/control/pause", "/control/resume"]:
            r = c.post(path, data={})  # no password
            assert r.status_code == 302, f"{path} -> {r.status_code} (expected redirect)"
            assert "control=badpass" in r.headers.get("Location", ""), r.headers.get("Location")
            r = c.post(path, data={"password": "wrong"})
            assert "control=badpass" in r.headers.get("Location", ""), "wrong pw must be rejected"
        # the instance was never mutated
        assert not ictrl.is_paused(iid), "instance must stay unpaused after rejected control posts"
    return "control routes fail-closed; no mutation on bad password"


def scenario_cache_headers():
    """Live-monitoring pages must not be browser-cached."""
    with dash_env() as (_tmp, _inst):
        r = _client().get("/")
        assert "no-store" in r.headers.get("Cache-Control", ""), r.headers.get("Cache-Control")
    return "responses carry no-store cache headers"


def scenario_template_folder_absolute():
    """template_folder must be absolute so templates resolve regardless of how
    the app is launched (cwd / relative-vs-absolute `python dashboard/app.py`).
    A relative folder 500s the no_instances page when there is no active instance."""
    assert Path(dash.app.template_folder).is_absolute(), dash.app.template_folder
    assert (Path(dash.app.template_folder) / "no_instances.html").exists()
    return "template_folder is absolute (launch-invariant)"


SCENARIOS = [
    ("html routes render 200", scenario_html_routes),
    ("zero-instance empty state", scenario_no_instances),
    ("url_for endpoints resolve", scenario_url_for_stable),
    ("api surface + 404s", scenario_api),
    ("control fail-closed", scenario_control_fail_closed),
    ("no-store cache headers", scenario_cache_headers),
    ("template_folder absolute", scenario_template_folder_absolute),
]


def _main():
    failures = 0
    for label, fn in SCENARIOS:
        try:
            detail = fn()
            print(f"PASS  {label}  —  {detail}")
        except Exception as e:
            failures += 1
            import traceback
            print(f"FAIL  {label}  —  {type(e).__name__}: {e}")
            traceback.print_exc()
    print()
    print(f"{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
