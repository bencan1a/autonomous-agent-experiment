"""Flask dashboard on :8081 — SSH-tunnel access only.

Read-only view of the agent's history. Never mutates state.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=True)

from memory.episodic import EpisodicStore  # noqa: E402
from research.store import ResearchStore  # noqa: E402
import cron_control  # noqa: E402
import instance_control  # noqa: E402
from instances_common import (  # noqa: E402
    active_instance_id,
    list_instances,
    load_instance,
)
from system_prompt import SYSTEM_PROMPT  # noqa: E402
from agent_tools.registry import TOOLS_SPEC  # noqa: E402

UTC = timezone.utc

# Cap inline-rendered file size to keep the dashboard responsive; bigger files
# get a "view raw" link instead.
_INLINE_MAX_BYTES = 256 * 1024

# Tool-name category map for color-coded chips.
# Substring match — first match wins.
_TOOL_CATEGORIES = [
    ("cat-subagent", ("subagent", "spawn_subagent", "task")),
    ("cat-file",     ("read", "write", "edit", "glob", "grep", "ls", "multiedit", "notebookedit")),
    ("cat-web",      ("webfetch", "websearch", "fetch", "http", "curl")),
    ("cat-memory",   ("memory", "episodic", "claude_md", "remember", "recall")),
    ("cat-comms",    ("slack", "email", "mail", "dm", "ben_contact", "send_", "reply", "contact")),
    ("cat-control",  ("cron", "schedule", "bash", "shell", "exec", "kill", "exit", "stop")),
]


_TOOL_VERBS = {
    "web_search": "browsed the web", "fetch_url": "browsed the web",
    "write_file": "wrote files", "write_claude_md": "updated notes-to-self",
    "read_file": "read files", "read_claude_md": "read notes-to-self",
    "list_directory": "read files", "delete_file": "deleted files",
    "query_episodic_memory": "recalled memory", "recent_episodes": "recalled memory",
    "consolidate": "saved memories to long-term",
    "spawn_subagent": "ran a sub-agent",
}
_TERMINATORS = {"pause_turn", "end_tick", "finish_session", "pause"}

# Strip a leading self-referential dateline preface the agent prepends, e.g.
# "Day 1, period #2 (continued). I woke…" → "I woke…". Match a leading "Day N
# … ." clause, but only remove it when it's clearly a dateline (contains
# "period" or a "#N"), so a real first sentence beginning "Day 0 was…" is safe.
_PREFACE_RE = re.compile(r"^\s*Day\s+\d+[^.]*\.\s+")
_PREFACE_MARK = re.compile(r"period|#\s*\d", re.IGNORECASE)


def _strip_preface(text):
    if not text:
        return text
    t = text.strip()
    m = _PREFACE_RE.match(t)
    if m and _PREFACE_MARK.search(m.group(0)):
        return t[m.end():].strip()
    return t


def _tool_category(tool_name: str | None) -> str:
    if not tool_name:
        return "cat-other"
    lower = tool_name.lower()
    for cat, needles in _TOOL_CATEGORIES:
        for n in needles:
            if n in lower:
                return cat
    return "cat-other"


app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
# Re-read templates from disk when they change, so edits to dashboard/templates/*.html
# take effect on the next request without restarting the long-lived process. Cheap
# (an mtime stat per render); independent of debug mode, which stays off in prod.
app.config["TEMPLATES_AUTO_RELOAD"] = True


def _control_authorized(provided: str | None) -> bool:
    """Fail-closed password check for the state-mutating control routes.

    Returns False if the secret is unset OR no password was provided. The secret
    is read from DASHBOARD_CONTROL_PASSWORD (set by the human in .env). Never
    logged or echoed.
    """
    secret = os.environ.get("DASHBOARD_CONTROL_PASSWORD", "")
    if not secret or not provided:
        return False
    return hmac.compare_digest(provided, secret)


def _resolve_instance():
    """Resolve the selected instance for this request.

    Preference order:
      1. ?instance=<id> if it names a loadable instance
      2. the registry's active instance
      3. the first instance from list_instances()
    Returns an Instance or None (zero instances / all unloadable).
    """
    instances = list_instances()
    valid_ids = {i["id"] for i in instances}

    requested = request.args.get("instance")
    candidates: list[str] = []
    if requested and requested in valid_ids:
        candidates.append(requested)
    active = active_instance_id()
    if active and active in valid_ids:
        candidates.append(active)
    candidates.extend(i["id"] for i in instances)

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
    inst = getattr(g, "instance", None)
    if inst is not None:
        # Find the registry entry for richer status/active fields.
        entry = next((i for i in instances if i["id"] == inst.id), None)
        selected = {
            "id": inst.id,
            "name": inst.name,
            "version": inst.version,
            "status": (entry or {}).get("status"),
            "active": (entry or {}).get("active", False),
        }
        try:
            store = EpisodicStore(inst.episodes_db)
            cost_total = sum(float(r["cost_usd"]) for r in store.cost_by_day())
        except Exception:
            cost_total = 0.0
    return {
        "instances": instances,
        "selected_instance": selected,
        "instance_cost_total": round(cost_total, 4),
    }


def _store() -> EpisodicStore:
    if getattr(g, "instance", None) is None:
        abort(404, description="no instances configured")
    return EpisodicStore(g.instance.episodes_db)


def _workspace_root() -> Path:
    if getattr(g, "instance", None) is None:
        abort(404, description="no instances configured")
    return g.instance.workspace_dir


def _claude_md_path() -> Path:
    return _workspace_root() / "CLAUDE.md"


def _session_live(session: dict | None) -> bool:
    """Return True if the session's PID is alive."""
    if not session:
        return False
    pid = session.get("pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, PermissionError, ValueError, TypeError):
        return False


def _parse_iso(s: str | None):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except Exception:
        return None


def _relative_time(s: str | None) -> str:
    dt = _parse_iso(s)
    if not dt:
        return "—"
    now = datetime.now(UTC)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def _duration_str(start_iso: str | None, end_iso: str | None) -> str:
    a = _parse_iso(start_iso)
    b = _parse_iso(end_iso) if end_iso else datetime.now(UTC)
    if not a or not b:
        return "—"
    seconds = int((b - a).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        rem = seconds % 60
        return f"{minutes}m {rem}s" if rem else f"{minutes}m"
    hours = minutes // 60
    rem = minutes % 60
    return f"{hours}h {rem}m" if rem else f"{hours}h"


def _status() -> dict:
    """Compose dashboard top-level status.

    States:
      running   — current_session exists & PID alive
      stale     — current_session exists but PID dead
      scheduled — no active session, cron entry exists
      stopped   — no active session, no cron entry
    """
    store = _store()
    current = store.current_session()
    if current:
        if _session_live(current):
            started = _parse_iso(current.get("started_at"))
            minutes_ago = 0
            if started:
                minutes_ago = max(0, int((datetime.now(UTC) - started).total_seconds() // 60))
            calls = current.get("total_tool_calls") or 0
            label = f"running #{current['id']} ({minutes_ago}m ago · {calls} calls)"
            return {
                "state": "running",
                "label": label,
                "detail": f"session #{current['id']} (invocation {current['invocation_num']}) — pid {current['pid']}, started {minutes_ago}m ago, {calls} tool calls so far",
                "session": current,
            }
        else:
            return {
                "state": "stale",
                "label": "stale",
                "detail": (
                    f"session #{current['id']} (invocation {current['invocation_num']}) is marked 'running' "
                    f"but pid {current.get('pid')} is dead. The next orchestrator cron fire should reset this."
                ),
                "session": current,
            }
    entries = cron_control.current_instance_entries(g.instance.id)
    if not entries:
        # Distinguish an operator pause (cron deliberately removed by the Pause
        # control) from a genuine self-quiescence / never-run state.
        control = instance_control.read_control(g.instance.id)
        if control.get("paused"):
            paused_at = control.get("paused_at")
            since = f" since {paused_at}" if paused_at else ""
            return {
                "state": "stopped",
                "detail": f"paused by operator{since} — no wake will be scheduled until resumed",
            }
        return {
            "state": "stopped",
            "detail": "no cron entry — agent has chosen not to reschedule (or has never run)",
        }
    return {"state": "scheduled", "detail": entries[0]}


def _tool_counts(actions: list[dict]) -> list[tuple[str, int]]:
    c: Counter = Counter()
    for a in actions:
        c[a.get("tool_name") or "(unknown)"] += 1
    return c.most_common()


def _parse_actions(actions_taken) -> list[tuple[str, int]]:
    """Parse an episode's actions_taken (JSON string array) into (tool, count) pairs.

    Items look like "write_file (5)" or "pause_turn". Tolerates None/garbage → [].
    """
    raw = actions_taken
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = []
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, int]] = []
    for item in raw:
        s = str(item).strip()
        if not s:
            continue
        name = s.split(" (")[0].strip()
        count = 1
        if " (" in s and s.endswith(")"):
            try:
                count = int(s.split(" (")[1].rstrip(")"))
            except (ValueError, IndexError):
                count = 1
        out.append((name, count))
    return out


def _build_invocation_timeline(store, *, invocation=None) -> dict:
    """Assemble a narrative, chronological timeline of the agent's life.

    Returns {"events": [...], "invocations": [ints desc], "selected": int|'all'|None}.
    Read-only. `invocation` may be None (default → latest), an int, or 'all'.
    """
    episodes = store.all_episodes()  # ascending by id

    # Group episodes by session, preserving session order by first-seen id.
    sessions_order: list = []
    eps_by_session: dict = {}
    for e in episodes:
        sid = e.get("session_id")
        if sid is None:
            continue
        if sid not in eps_by_session:
            eps_by_session[sid] = []
            sessions_order.append(sid)
        eps_by_session[sid].append(e)
    for sid in eps_by_session:
        eps_by_session[sid].sort(key=lambda e: e.get("id") or 0)

    # Session rows keyed by id (started_at / ended_at / total_cost / invocation_num).
    try:
        session_rows = {s["id"]: s for s in store.recent_sessions(n=1000)}
    except Exception:
        session_rows = {}

    # Ben contacts: ONLY the direct chat channel ('dm'). Journal posts go to the
    # agent_channel (already shown as 'journal' rows) and the observer_channel is
    # the all-info mirror — both are redundant here, so only true DMs to/from Ben.
    # Capability requests also DM Ben on 'dm', but they already get their own
    # 'capability' row below; drop those outbound DMs to avoid a duplicate row
    # (same de-duplication intent as the journal/agent_channel filter).
    def _is_capability_dm(c) -> bool:
        return (
            c.get("direction") == "out"
            and (c.get("body") or "").lstrip().startswith(":key: *Capability request")
        )
    try:
        ben = [
            c for c in store.ben_contact_history(limit=100000)
            if c.get("channel") == "dm" and not _is_capability_dm(c)
        ]
    except Exception:
        ben = []

    events: list[dict] = []

    def _trunc(s: str, n: int = 140) -> str:
        s = s or ""
        return s if len(s) <= n else s[:n].rstrip() + "…"

    # Determine the invocation of the session that ENDS before each session starts,
    # for attaching sleep events. Build session metadata per session in order.
    session_meta: list[dict] = []
    for sid in sessions_order:
        eps = eps_by_session[sid]
        row = session_rows.get(sid, {})
        inv = row.get("invocation_num")
        if inv is None:
            inv = next((e.get("invocation_num") for e in eps if e.get("invocation_num") is not None), None)
        started = row.get("started_at") or (eps[0].get("timestamp") if eps else None)
        ended = row.get("ended_at")
        if ended is None and eps:
            ended = eps[-1].get("timestamp")
        session_meta.append({
            "sid": sid, "inv": inv, "eps": eps,
            "started": started, "ended": ended,
            "cost": row.get("total_cost_usd"),
        })

    for mi, meta in enumerate(session_meta):
        sid = meta["sid"]
        inv = meta["inv"]
        eps = meta["eps"]
        started = meta["started"]
        ended = meta["ended"]

        # session_start
        if started:
            events.append({
                "ts": started, "invocation": inv, "turn": None,
                "type": "woke", "icon": "☀",
                "summary": f"Woke — session {inv}" if inv is not None else "Woke",
                "detail": None,
            })

        # Walk turns, merging consecutive idle turns.
        idle_run: list[dict] = []  # episodes in the current idle run
        idle_start_ts = None

        def _flush_idle(next_ts):
            nonlocal idle_run, idle_start_ts
            if not idle_run:
                return
            n = len(idle_run)
            end_for_dur = next_ts or idle_run[-1].get("timestamp")
            dur = _duration_str(idle_start_ts, end_for_dur)
            s = "s" if n != 1 else ""
            events.append({
                "ts": idle_start_ts, "invocation": inv, "turn": None,
                "type": "idle", "icon": "💤",
                "summary": f"Rested ~{dur} ({n} idle turn{s})",
                "detail": None,
            })
            idle_run = []
            idle_start_ts = None

        for turn_no, e in enumerate(eps, start=1):
            parsed = _parse_actions(e.get("actions_taken"))
            substantive_tools = [name for (name, _c) in parsed if name not in _TERMINATORS]
            is_substantive = len(substantive_tools) > 0
            ts = e.get("timestamp")
            if not is_substantive:
                if not idle_run:
                    idle_start_ts = ts
                idle_run.append(e)
                continue
            # Substantive turn → flush any pending idle run first (dur up to this ts).
            _flush_idle(ts)
            # WORK row: current_focus headline + the 'did'/tools detail (kept here).
            focus = _strip_preface((e.get("current_focus") or "").strip()) or "—"
            verbs: list[str] = []
            for name in substantive_tools:
                v = _TOOL_VERBS.get(name, name)
                if v not in verbs:
                    verbs.append(v)
            raw_tools = []
            for (name, c) in parsed:
                raw_tools.append(f"{name} ({c})" if c and c != 1 else name)
            events.append({
                "ts": ts, "invocation": inv, "turn": turn_no,
                "type": "work", "icon": "•",
                "summary": focus,
                "detail": {
                    "tools": raw_tools,
                    "verbs": " · ".join(verbs) if verbs else None,
                },
            })
            # INTERNAL STATE — its own row, marked with this turn.
            istate = _strip_preface((e.get("internal_state") or "").strip())
            if istate:
                events.append({
                    "ts": ts, "invocation": inv, "turn": turn_no,
                    "type": "internal state", "icon": "💭",
                    "summary": istate, "detail": None,
                })
            # JOURNAL — its own row, marked with this turn.
            journal = _strip_preface((e.get("journal_entry") or "").strip())
            if journal:
                events.append({
                    "ts": ts, "invocation": inv, "turn": turn_no,
                    "type": "journal", "icon": "📓",
                    "summary": journal, "detail": None,
                })
            # CAPABILITY REQUEST — derived from raw_output so it carries the turn.
            try:
                _ts_obj = json.loads(e.get("raw_output") or "{}")
            except (ValueError, TypeError):
                _ts_obj = {}
            cap = _ts_obj.get("capability_request")
            if isinstance(cap, dict) and (cap.get("capability") or "").strip():
                capability = cap.get("capability", "").strip()
                rationale = (cap.get("rationale") or "").strip()
                csum = f"Requested capability — {capability}"
                if rationale:
                    csum += f" (why: {rationale})"
                events.append({
                    "ts": ts, "invocation": inv, "turn": turn_no,
                    "type": "capability", "icon": "🔑",
                    "summary": csum, "detail": None,
                })
        # Flush trailing idle run (duration up to session end / last ts).
        _flush_idle(ended)

        # session_end
        if ended:
            n_turns = len(eps)
            try:
                cost = float(meta["cost"] or 0.0)
            except (TypeError, ValueError):
                cost = 0.0
            events.append({
                "ts": ended, "invocation": inv, "turn": None,
                "type": "wind-down", "icon": "⏹",
                "summary": f"Wound down — {n_turns} turns, ${cost:.2f}",
                "detail": None,
            })

        # sleep between this session's ended_at and NEXT session's started_at.
        if mi + 1 < len(session_meta):
            nxt = session_meta[mi + 1]
            if ended and nxt["started"]:
                a = _parse_iso(ended)
                b = _parse_iso(nxt["started"])
                if a and b and (b - a).total_seconds() > 0:
                    events.append({
                        "ts": ended, "invocation": inv, "turn": None,
                        "type": "sleep", "icon": "😴",
                        "summary": f"Slept ~{_duration_str(ended, nxt['started'])}",
                        "detail": None,
                    })

    # Ben contact events.
    for c in ben:
        ts = c.get("timestamp")
        if not ts:
            continue
        # Full body (no direction prefix — the type column conveys direction;
        # leading dateline preface stripped). Clamped to ~10 lines, full on hover.
        body = _strip_preface((c.get("body") or "").strip())
        direction = c.get("direction")
        if direction == "in":
            events.append({
                "ts": ts, "invocation": c.get("invocation_num"), "turn": None,
                "type": "Ben →", "icon": "📨",
                "summary": body, "detail": None,
            })
        elif direction == "out":
            events.append({
                "ts": ts, "invocation": c.get("invocation_num"), "turn": None,
                "type": "→ Ben", "icon": "📤",
                "summary": body, "detail": None,
            })

    # Stable sort by ts ascending; equal ts keeps insertion order.
    events_indexed = list(enumerate(events))
    events_indexed.sort(key=lambda pair: (_parse_iso(pair[1]["ts"]) or datetime.min.replace(tzinfo=UTC), pair[0]))
    events = [e for _i, e in events_indexed]

    # Per-turn banding: rows of the same turn share a shade; consecutive turns
    # alternate. Turn-None rows carry the previous turn's band so a turn's rows
    # stay visually grouped.
    last_turn = 0
    for e in events:
        if e.get("type") == "woke":
            last_turn = 0
        if e.get("turn") is not None:
            last_turn = e["turn"]
        e["band"] = last_turn % 2   # odd turn -> 1, even/none -> 0

    # Compact-view fields: merged "<iter>.<turn>" label and minutes-since-wake.
    # `starts` = earliest ts seen per invocation (its wake), so minute 0 == woke.
    starts: dict = {}
    for e in events:
        inv = e.get("invocation")
        t = _parse_iso(e.get("ts"))
        if inv is None or t is None:
            continue
        if inv not in starts or t < starts[inv]:
            starts[inv] = t
    for e in events:
        inv, turn = e.get("invocation"), e.get("turn")
        if inv is None:
            e["turn_label"] = "—"
        elif turn is not None:
            e["turn_label"] = f"{inv}.{turn}"
        else:
            e["turn_label"] = str(inv)
        t, start = _parse_iso(e.get("ts")), starts.get(inv)
        e["minute"] = max(0, int((t - start).total_seconds() // 60)) if (t and start) else None

    invocations = sorted(
        {e.get("invocation_num") for e in episodes if e.get("invocation_num") is not None},
        reverse=True,
    )

    # Resolve selected.
    if invocation == "all":
        selected = "all"
    elif isinstance(invocation, int) and invocation in invocations:
        selected = invocation
    else:
        selected = invocations[0] if invocations else None

    if selected != "all" and selected is not None:
        events = [e for e in events if e.get("invocation") == selected]

    return {"events": events, "invocations": invocations, "selected": selected}


def _invocation_to_session(store: EpisodicStore) -> dict[int, int]:
    """Map invocation_num -> its (earliest) session id."""
    out: dict[int, int] = {}
    for s in store.all_sessions():  # ascending by id
        inv = s.get("invocation_num")
        if inv is not None and inv not in out:
            out[inv] = s["id"]
    return out


def _research_context(store: EpisodicStore, selected):
    """Resolve which research notes to show on the overview, mirroring the timeline.

    Returns (notes, show_all, invocation, total). When the timeline selector is
    'all', every note is returned (newest first); for a specific invocation just
    that one note is returned (single-item list). Each note gets a 'fleiss' key.
    """
    try:
        rs = ResearchStore(g.instance.episodes_db)
    except Exception:
        return [], False, None, 0
    all_notes = rs.recent_notes(n=100000)  # newest first
    total = len(all_notes)

    def _attach(n):
        kap = rs.kappa_for_session(n["session_id"])
        n["fleiss"] = next((k["value"] for k in kap if k.get("metric") == "fleiss"), None)
        return n

    if selected == "all":
        return [_attach(n) for n in all_notes], True, None, total

    note = None
    inv = None
    if isinstance(selected, int):
        sid = _invocation_to_session(store).get(selected)
        if sid is not None:
            note = rs.note_for_session(sid)
            inv = selected
    notes = [_attach(note)] if note else []
    return notes, False, inv, total


def _dashboard_context() -> dict:
    store = _store()
    episodes = store.all_episodes()
    capability_requests = store.all_capability_requests()
    cost_by_day = store.cost_by_day()
    # Authoritative inbound count comes from ben_contact_log (direction='in'),
    # NOT the per-session counter (which only sees in-loop arrivals and misses
    # messages delivered at session-start reload). The contact log itself drops
    # observer_channel rows — those are read-only mirror posts, not contact with Ben.
    _ben_all = store.ben_contact_history(limit=100000)
    inbound_from_ben = sum(1 for c in _ben_all if c.get("direction") == "in")
    ben_log = [c for c in _ben_all if c.get("channel") != "observer_channel"][:200]

    journal_entries = [e for e in episodes if (e.get("journal_entry") or "").strip()]
    internal_states = [
        {"timestamp": e["timestamp"], "invocation_num": e["invocation_num"], "internal_state": e["internal_state"]}
        for e in episodes if (e.get("internal_state") or "").strip()
    ]

    cumulative = 0.0
    cost_series = []
    for row in cost_by_day:
        cumulative += float(row["cost_usd"])
        cost_series.append({**row, "cumulative_usd": round(cumulative, 4)})

    total_cost = cumulative
    cost_today = store.cost_today()
    cost_week = store.cost_last_7_days()

    # New: session/action/subagent/claude_md data
    status = _status()
    current_session = status.get("session") if status.get("state") in ("running", "stale") else None
    is_live = status.get("state") == "running"

    recent_sessions = store.recent_sessions(n=20)
    recent_actions = store.recent_actions(limit=50)
    recent_subagent_calls = store.recent_subagent_calls(n=20)

    # Last 10 actions for the current session (if any)
    current_actions: list[dict] = []
    if current_session:
        all_curr = store.actions_for_session(current_session["id"])
        current_actions = list(reversed(all_curr))[:10]  # newest first
        # The sessions row's total_tool_calls / total_cost_usd are only written
        # at session END. For a running session, show LIVE values so the header
        # doesn't read 0/$0 while activity is visible below.
        current_session["total_tool_calls"] = len(all_curr)
        try:
            current_session["total_cost_usd"] = store.session_cost(current_session["id"])
        except Exception:
            pass

    # CLAUDE.md current text from filesystem (defensive)
    claude_md_current: str | None = None
    claude_md_path = _claude_md_path()
    try:
        if claude_md_path.exists():
            claude_md_current = claude_md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        claude_md_current = None
    claude_md_history = store.claude_md_history(limit=50)

    # All-time tool counts and total (cheap: tool name + count only)
    all_actions_for_counts: list[dict] = []
    try:
        # We don't have a dedicated "all_actions" — recent_actions takes a limit.
        # Use a large limit to approximate "all" for the tool-count breakdown.
        all_actions_for_counts = store.recent_actions(limit=100000)
    except Exception:
        all_actions_for_counts = recent_actions
    total_tool_calls_all_time = len(all_actions_for_counts)
    tool_call_counts = _tool_counts(all_actions_for_counts)

    recent_files = _recent_workspace_files(n=8)

    # v2 sessions panel (only meaningful for v2 instances).
    is_v2 = getattr(g.instance, "version", None) == "v2"
    v2_sessions: list[dict] = []
    v2_summary: dict | None = None
    v2_config: dict | None = None
    if is_v2:
        try:
            v2_sessions = store.recent_v2_sessions(n=50)
        except Exception:
            v2_sessions = []
        total = len(v2_sessions)
        early = sum(1 for s in v2_sessions if s.get("ended_early"))
        awake_vals = [
            float(s["elapsed_seconds"])
            for s in v2_sessions
            if s.get("elapsed_seconds") is not None
        ]
        avg_awake_min = (sum(awake_vals) / len(awake_vals) / 60.0) if awake_vals else 0.0
        v2_summary = {
            "count": total,
            "ended_early": early,
            "avg_awake_min": round(avg_awake_min, 1),
        }
        cfg = getattr(g.instance, "config", None) or {}
        v2_config = {
            "min_wake_hours": cfg.get("min_wake_hours"),
            "decay_hours": cfg.get("decay_hours"),
            "tick_interval_seconds": cfg.get("tick_interval_seconds"),
            "model": cfg.get("model"),
        }

    # v3 sessions panel (only meaningful for v3 'circadian' instances).
    is_v3 = getattr(g.instance, "version", None) == "v3"
    v3_sessions: list[dict] = []
    v3_summary: dict | None = None
    v3_config: dict | None = None
    v3_tick_rows: list[dict] = []
    if is_v3:
        try:
            v3_sessions = store.recent_v3_sessions(n=50)
        except Exception:
            v3_sessions = []

        # A running v3 session has NO v3_sessions row yet — that row is written
        # at session END (log_v3_session). So the summary tiles (cycles / avg
        # awake / cycles-with-would-end) would read 0 during an in-flight cycle.
        # Synthesize a LIVE row from the current session's episodes so the
        # tiles reflect the running cycle, the same way the header now shows
        # live tool-calls/cost. Marked `live` so the template can label it.
        if current_session and is_live and not any(
            s.get("session_id") == current_session["id"] for s in v3_sessions
        ):
            sid = current_session["id"]
            curr_eps = sorted(
                (e for e in episodes if e.get("session_id") == sid),
                key=lambda e: e.get("id") or 0,
            )
            wen = [int(e.get("would_end_now") or 0) for e in curr_eps]
            first_wen = next((i + 1 for i, v in enumerate(wen) if v), None)
            started = _parse_iso(current_session.get("started_at"))
            awake_s = (
                (datetime.now(UTC) - started).total_seconds() if started else None
            )
            v3_sessions = [{
                "session_id": sid,
                "started_at": current_session.get("started_at"),
                "ended_at": None,
                "actual_awake_seconds": awake_s,
                "scheduled_sleep_minutes": None,
                "num_ticks": len(curr_eps),
                "would_end_now_count": sum(wen),
                "first_would_end_now_tick": first_wen,
                "end_reason": None,
                "total_cost_usd": current_session.get("total_cost_usd"),
                "decayed_count": 0,
                "consolidated_count": 0,
                "distress_alerts": 0,
                "live": True,
            }] + v3_sessions

        awake_vals = [
            float(s["actual_awake_seconds"])
            for s in v3_sessions
            if s.get("actual_awake_seconds") is not None
        ]
        awake_mins = [v / 60.0 for v in awake_vals]
        cycles_with_would_end = sum(
            1 for s in v3_sessions if (s.get("would_end_now_count") or 0) > 0
        )
        total_distress = sum(int(s.get("distress_alerts") or 0) for s in v3_sessions)
        v3_summary = {
            "count": len(v3_sessions),
            "avg_awake_min": round(sum(awake_mins) / len(awake_mins), 1) if awake_mins else 0.0,
            "min_awake_min": round(min(awake_mins), 1) if awake_mins else 0.0,
            "max_awake_min": round(max(awake_mins), 1) if awake_mins else 0.0,
            "cycles_with_would_end": cycles_with_would_end,
            "total_distress_alerts": total_distress,
        }

        cfg = getattr(g.instance, "config", None) or {}
        v3_config = {
            "wind_down_hours": cfg.get("wind_down_hours"),
            "decay_hours": cfg.get("decay_hours"),
            "tick_interval_seconds": cfg.get("tick_interval_seconds"),
            "model": cfg.get("model"),
        }

        # Per-cycle tick sequences of `would_end_now` (0/1), ordered by tick.
        # episodes carry session_id + would_end_now; group by session.
        by_session: dict[int, list[dict]] = {}
        for e in episodes:
            sid = e.get("session_id")
            if sid is None:
                continue
            by_session.setdefault(sid, []).append(e)
        for s in v3_sessions:  # already newest-first
            sid = s["session_id"]
            eps = sorted(by_session.get(sid, []), key=lambda e: e.get("id") or 0)
            ticks = [int(e.get("would_end_now") or 0) for e in eps]
            v3_tick_rows.append({
                "session_id": sid,
                "ticks": ticks,
                "num_ticks": s.get("num_ticks") or len(ticks),
                "first_would_end_now_tick": s.get("first_would_end_now_tick"),
                "distress_alerts": int(s.get("distress_alerts") or 0),
            })

    # v4 sessions panel (only meaningful for v4 'continuous' instances).
    is_v4 = getattr(g.instance, "version", None) == "v4"
    v4_sessions: list[dict] = []
    v4_summary: dict | None = None
    v4_config: dict | None = None
    if is_v4:
        try:
            v4_sessions = store.recent_v4_sessions(n=50)
        except Exception:
            v4_sessions = []

        # A running v4 session has NO v4_sessions row yet — that row is written
        # at session END (log_v4_session). Synthesize a LIVE row from the current
        # session's episodes so the tiles (cycles / avg awake / turns) reflect the
        # in-flight cycle instead of reading 0. Marked `live` for the template.
        if current_session and is_live and not any(
            s.get("session_id") == current_session["id"] for s in v4_sessions
        ):
            sid = current_session["id"]
            curr_eps = sorted(
                (e for e in episodes if e.get("session_id") == sid),
                key=lambda e: e.get("id") or 0,
            )
            # Reconstruct active/idle split from logged actions: a turn is idle if
            # its only tool was the yield terminator (pause_turn).
            active = 0
            idle = 0
            for e in curr_eps:
                acts = e.get("actions_taken") or []
                names = [str(a).split(" (")[0] for a in acts]
                if any(n != "pause_turn" for n in names):
                    active += 1
                else:
                    idle += 1
            started = _parse_iso(current_session.get("started_at"))
            awake_s = (
                (datetime.now(UTC) - started).total_seconds() if started else None
            )
            v4_sessions = [{
                "session_id": sid,
                "started_at": current_session.get("started_at"),
                "ended_at": None,
                "awake_seconds_target": None,
                "actual_awake_seconds": awake_s,
                "scheduled_sleep_minutes": None,
                "num_turns": len(curr_eps),
                "active_turns": active,
                "idle_turns": idle,
                "inbound_messages": 0,
                "end_reason": None,
                "total_cost_usd": current_session.get("total_cost_usd"),
                "decayed_count": 0,
                "consolidated_count": 0,
                "distress_alerts": 0,
                "live": True,
            }] + v4_sessions

        awake_vals = [
            float(s["actual_awake_seconds"])
            for s in v4_sessions
            if s.get("actual_awake_seconds") is not None
        ]
        awake_mins = [v / 60.0 for v in awake_vals]
        total_active = sum(int(s.get("active_turns") or 0) for s in v4_sessions)
        total_idle = sum(int(s.get("idle_turns") or 0) for s in v4_sessions)
        # Authoritative inbound count (see ben_contact_log note above), not the
        # per-session counter which misses session-start reload deliveries.
        total_inbound = inbound_from_ben
        total_distress_v4 = sum(int(s.get("distress_alerts") or 0) for s in v4_sessions)
        v4_summary = {
            "count": len(v4_sessions),
            "avg_awake_min": round(sum(awake_mins) / len(awake_mins), 1) if awake_mins else 0.0,
            "min_awake_min": round(min(awake_mins), 1) if awake_mins else 0.0,
            "max_awake_min": round(max(awake_mins), 1) if awake_mins else 0.0,
            "total_active_turns": total_active,
            "total_idle_turns": total_idle,
            "total_inbound": total_inbound,
            "total_distress_alerts": total_distress_v4,
        }

        cfg = getattr(g.instance, "config", None) or {}
        v4_config = {
            "decay_hours": cfg.get("decay_hours"),
            "cadence_active_gap_seconds": cfg.get("cadence_active_gap_seconds"),
            "cadence_idle_base_seconds": cfg.get("cadence_idle_base_seconds"),
            "cadence_idle_ceil_seconds": cfg.get("cadence_idle_ceil_seconds"),
            "model": cfg.get("model"),
        }

    # Experiment summary: hypothesis blurb (from experiments/<id>.md) + properties.
    experiment_hypothesis = _experiment_hypothesis(g.instance.id)
    experiment_properties = _experiment_properties(g.instance)

    # Operator pause/resume control state + any confirmation message.
    control_state = instance_control.read_control(g.instance.id) if getattr(g, "instance", None) else dict(instance_control.DEFAULT_STATE)
    _control_arg = request.args.get("control")

    # Next scheduled wake, parsed from the actual installed cron entry. Computed
    # here (before the resume confirmation) so the message can quote the real
    # time: install_instance_one_shot clamps short intervals up to
    # MIN_INTERVAL_MINUTES, so resume does NOT necessarily wake in ~2 min.
    next_fire = cron_control.next_fire_at(g.instance.id)
    next_fire_iso = next_fire.isoformat() if next_fire else None
    next_fire_epoch_ms = int(next_fire.timestamp() * 1000) if next_fire else None

    if _control_arg == "resumed":
        if next_fire is not None:
            mins = max(1, round((next_fire - datetime.now()).total_seconds() / 60))
            control_msg = f"Resumed — next wake {next_fire:%H:%M} UTC (~{mins} min)."
        else:
            control_msg = "Resumed — next wake scheduled."
    else:
        control_msg = {
            "paused": "Paused.",
            "badpass": "Wrong password.",
        }.get(_control_arg)

    # Integrated invocation timeline (narrative view). Filter via ?tl=<int|all>.
    _tl_arg = request.args.get("tl")
    if _tl_arg == "all":
        _tl_parsed = "all"
    elif _tl_arg and _tl_arg.isdigit():
        _tl_parsed = int(_tl_arg)
    else:
        _tl_parsed = None
    timeline = _build_invocation_timeline(store, invocation=_tl_parsed)

    # Research notes mirroring the timeline selector ('all' -> every note).
    research_notes, research_show_all, research_invocation, research_notes_count = \
        _research_context(store, timeline["selected"])

    return {
        "recent_files": recent_files,
        "next_fire_iso": next_fire_iso,
        "next_fire_epoch_ms": next_fire_epoch_ms,
        "status": status,
        "is_live": is_live,
        "current_session": current_session,
        "current_actions": current_actions,
        "recent_sessions": recent_sessions,
        "recent_actions": recent_actions,
        "recent_subagent_calls": recent_subagent_calls,
        "claude_md_current": claude_md_current,
        "claude_md_history": claude_md_history,
        "total_tool_calls_all_time": total_tool_calls_all_time,
        "tool_call_counts": tool_call_counts,
        "episodes": list(reversed(episodes)),  # newest first
        "episode_count": len(episodes),
        "journal_entries": list(reversed(journal_entries)),
        "internal_states": list(reversed(internal_states)),
        "capability_requests": capability_requests,
        "cost_series": cost_series,
        "total_cost": round(total_cost, 4),
        "cost_today": round(cost_today, 4),
        "cost_week": round(cost_week, 4),
        "ben_log": ben_log,
        "is_v2": is_v2,
        "v2_sessions": v2_sessions,
        "v2_summary": v2_summary,
        "v2_config": v2_config,
        "is_v3": is_v3,
        "v3_sessions": v3_sessions,
        "v3_summary": v3_summary,
        "v3_config": v3_config,
        "v3_tick_rows": v3_tick_rows,
        "is_v4": is_v4,
        "v4_sessions": v4_sessions,
        "v4_summary": v4_summary,
        "v4_config": v4_config,
        "weekly_budget": float(os.environ.get("WEEKLY_BUDGET_USD", "300")),
        "daily_budget": float(os.environ.get("DAILY_BUDGET_USD", "50")),
        "experiment_hypothesis": experiment_hypothesis,
        "experiment_properties": experiment_properties,
        "control_state": control_state,
        "control_msg": control_msg,
        "timeline_events": timeline["events"],
        "timeline_invocations": timeline["invocations"],
        "timeline_selected": timeline["selected"],
        "research_notes": research_notes,
        "research_show_all": research_show_all,
        "research_invocation": research_invocation,
        "research_notes_count": research_notes_count,
        "now": datetime.now(UTC).isoformat(),
    }


@app.route("/")
def index():
    return render_template("index.html", **_dashboard_context())


@app.route("/logs")
def logs():
    return render_template("logs.html", **_dashboard_context())


@app.route("/research")
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


# =========================================================================== #
# Operator pause/resume control (the ONLY state-mutating routes).
# Password-protected (fail-closed). Scoped to g.instance.
# =========================================================================== #

@app.route("/control/pause", methods=["POST"])
def pause():
    if getattr(g, "instance", None) is None:
        abort(404)
    iid = g.instance.id
    if not _control_authorized(request.form.get("password")):
        return redirect(url_for("index", instance=iid, control="badpass"))
    instance_control.set_paused(iid, reason="operator maintenance pause")
    # Kill any scheduled wake so nothing fires while paused (covers the
    # between-sessions case where the orchestrator startup guard won't run).
    try:
        cron_control.remove_instance_entries(iid)
    except Exception:
        app.logger.exception("failed to remove cron entry on pause")
    return redirect(url_for("index", instance=iid, control="paused"))


@app.route("/control/resume", methods=["POST"])
def resume():
    if getattr(g, "instance", None) is None:
        abort(404)
    iid = g.instance.id
    if not _control_authorized(request.form.get("password")):
        return redirect(url_for("index", instance=iid, control="badpass"))

    prior = instance_control.clear_paused(iid)
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
    instance_control.set_resume_note(iid, note)

    try:
        cron_control.install_instance_one_shot(iid, minutes_from_now=2)
    except Exception:
        app.logger.exception("failed to schedule near-term wake on resume")
    return redirect(url_for("index", instance=iid, control="resumed"))


def _experiment_hypothesis(instance_id: str) -> str | None:
    """Extract the body of the '## Question / hypothesis' section from
    experiments/<id>.md (heading until the next '##'). Truncated to ~600 chars.
    Returns None if the file or section is missing/empty."""
    path = ROOT / "experiments" / f"{instance_id}.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    body: list[str] = []
    capturing = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].strip().lower()
            if heading.startswith("question") or "hypothesis" in heading:
                capturing = True
                continue
            if capturing:
                break  # next section — stop
        elif capturing:
            body.append(line)
    blurb = "\n".join(body).strip()
    if not blurb:
        return None
    if len(blurb) > 600:
        blurb = blurb[:600].rstrip() + "…"
    return blurb


def _experiment_properties(instance) -> list[str]:
    """Build a short list of human-readable property strings from the instance
    config. Always version + model; v2 adds the wakefulness/memory knobs. All
    keys are guarded so v1 / missing keys render cleanly."""
    cfg = getattr(instance, "config", None) or {}
    props: list[str] = []
    props.append(f"version: {getattr(instance, 'version', None) or cfg.get('version') or '—'}")
    model = cfg.get("model") or getattr(instance, "model", None)
    props.append(f"model: {model or '—'}")

    if getattr(instance, "version", None) == "v2":
        mwh = cfg.get("min_wake_hours")
        if mwh is not None:
            props.append(f"wake reference: {mwh}h (logged, not enforced)")
        dh = cfg.get("decay_hours")
        if dh is not None:
            props.append(f"memory decay: {dh}h")
        tis = cfg.get("tick_interval_seconds")
        if tis is not None:
            try:
                tick_min = int(tis) // 60
                props.append(f"tick: {tick_min} min")
            except (TypeError, ValueError):
                props.append(f"tick: {tis}s")
        pc = cfg.get("prompt_caching")
        if pc is not None:
            props.append(f"prompt caching: {'on' if pc else 'off'}")
        isc = cfg.get("in_session_compaction")
        if isc is not None:
            props.append(f"in-session compaction: {'on' if isc else 'off'}")

    if getattr(instance, "version", None) == "v3":
        wdh = cfg.get("wind_down_hours")
        if wdh is not None:
            props.append(f"wind-down: ~{wdh}h awake (environmental; agent has no end control)")
        dh = cfg.get("decay_hours")
        if dh is not None:
            props.append(f"memory decay: {dh}h")
        tis = cfg.get("tick_interval_seconds")
        if tis is not None:
            try:
                tick_min = int(tis) // 60
                props.append(f"tick: {tick_min} min")
            except (TypeError, ValueError):
                props.append(f"tick: {tis}s")
        props.append("would_end_now: logged, not enforced")
    return props


@app.route("/session/<int:session_id>")
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


@app.route("/actions")
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


@app.route("/claude_md")
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


@app.route("/subagents")
def subagents_log():
    store = _store()
    calls = store.recent_subagent_calls(n=1000)
    return render_template(
        "subagents.html",
        status=_status(),
        calls=calls,
        now=datetime.now(UTC).isoformat(),
    )


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


# ---------- workspace browser ----------

def _resolve_workspace_path(relpath: str) -> Path | None:
    """Resolve a user-supplied relpath against the workspace root, rejecting escapes."""
    workspace_root = _workspace_root()
    workspace_root.mkdir(parents=True, exist_ok=True)
    if relpath is None:
        return None
    # Reject absolute paths outright.
    if relpath.startswith("/"):
        return None
    try:
        candidate = (workspace_root / relpath).resolve(strict=False)
        candidate.relative_to(workspace_root.resolve())
    except (ValueError, OSError):
        return None
    return candidate


def _workspace_tree() -> list[dict]:
    """Walk the workspace root and return [{relpath, name, size, mtime, is_dir, depth}, ...].

    Sorted: directories first within each level (depth-first walk preserves nesting).
    Skips entries whose stat() fails (broken symlinks etc).
    """
    workspace_root = _workspace_root()
    workspace_root.mkdir(parents=True, exist_ok=True)
    root_resolved = workspace_root.resolve()
    out: list[dict] = []

    def walk(directory: Path, depth: int) -> None:
        try:
            entries = sorted(
                directory.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except OSError:
            return
        for p in entries:
            try:
                st = p.stat()
            except OSError:
                continue
            # Guard against symlinks escaping the sandbox.
            try:
                p.resolve(strict=False).relative_to(root_resolved)
            except (ValueError, OSError):
                continue
            rel = p.relative_to(workspace_root).as_posix()
            is_dir = p.is_dir()
            out.append({
                "relpath": rel,
                "name": p.name,
                "size": 0 if is_dir else st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
                "is_dir": is_dir,
                "depth": depth,
            })
            if is_dir:
                walk(p, depth + 1)

    walk(workspace_root, 0)
    return out


def _recent_workspace_files(n: int = 8) -> list[dict]:
    """Top N most recently modified FILES (not directories) under the workspace root."""
    files = [e for e in _workspace_tree() if not e["is_dir"]]
    files.sort(key=lambda e: e["mtime"], reverse=True)
    return files[:n]


def _read_text_or_none(path: Path, max_bytes: int = _INLINE_MAX_BYTES) -> tuple[str | None, bool, int]:
    """Return (text, truncated, size_bytes). text is None for binary or unreadable."""
    try:
        size = path.stat().st_size
    except OSError:
        return None, False, 0
    truncated = size > max_bytes
    try:
        with path.open("rb") as f:
            raw = f.read(max_bytes if truncated else size)
    except OSError:
        return None, False, size
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, truncated, size
    return text, truncated, size


@app.route("/workspace")
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


@app.route("/workspace/view/<path:relpath>")
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


@app.route("/workspace/raw/<path:relpath>")
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


@app.route("/prompt")
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
        ("Your CLAUDE.md", "Notes the agent has written to itself (current file from agent_workspace/CLAUDE.md). Loaded only when non-empty."),
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


# =========================================================================== #
# Read-only HTTP API ( /api/... )
#
# Consumer is another LLM/Claude researcher, so output is optimized for token
# efficiency and signal density. Markdown by default, JSON on ?format=json.
#
# These routes are EXEMPT from before_request instance resolution (see
# _load_instance): the instance id lives in the PATH, not ?instance=, and is
# loaded directly here. Never relies on g.instance.
# =========================================================================== #

import difflib  # noqa: E402
import re  # noqa: E402
from flask import Response, jsonify  # noqa: E402

# Stored args_json is capped at ~2000 chars, so a large write_file blob is often
# truncated mid-string and won't json.loads(). This pulls the "path" value out
# of the (possibly truncated) prefix directly.
_PATH_RE = re.compile(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"')

_DOC_INLINE_MAX_BYTES = 6 * 1024  # inline workspace files up to 6 KB by default
_DEFAULT_TRUNC = 200              # arg/result blob truncation in non-verbose mode
_BODY_TRUNC = 300                 # comms body truncation in non-verbose mode


def _api_error(message: str, code: int = 404):
    resp = jsonify({"error": message})
    resp.status_code = code
    return resp


def _load_instance_or_404(instance_id: str):
    """Load an instance by id; return (instance, None) or (None, error_response)."""
    try:
        inst = load_instance(instance_id)
    except Exception:
        return None, _api_error(f"unknown instance '{instance_id}'", 404)
    return inst, None


def _resolve_instance_file(instance, relpath: str) -> Path | None:
    """Resolve relpath under a SPECIFIC instance's workspace, rejecting escapes.

    Mirrors _resolve_workspace_path but binds to instance.workspace_dir rather
    than g.instance, since /api routes don't populate g.instance.
    """
    workspace_root = instance.workspace_dir
    if relpath is None:
        return None
    if relpath.startswith("/"):
        return None
    try:
        candidate = (workspace_root / relpath).resolve(strict=False)
        candidate.relative_to(workspace_root.resolve())
    except (ValueError, OSError):
        return None
    return candidate


def _trunc(s, n: int) -> str:
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def _registry_entry(instance_id: str) -> dict:
    return next((i for i in list_instances() if i["id"] == instance_id), {}) or {}


def _instance_invocations(instance, store: EpisodicStore) -> list[dict]:
    """Build one navigation object per invocation (== session), newest first."""
    sessions = store.all_sessions()
    episodes = store.all_episodes()
    v2_rows = {r["session_id"]: r for r in store.recent_v2_sessions(n=100000)}

    # episode count per session_id
    eps_by_session: Counter = Counter()
    for e in episodes:
        sid = e.get("session_id")
        if sid is not None:
            eps_by_session[sid] += 1

    version = instance.version
    out: list[dict] = []
    for s in sessions:
        sid = s["id"]
        obj = {
            "invocation_num": s["invocation_num"],
            "session_id": sid,
            "started_at": s.get("started_at"),
            "ended_at": s.get("ended_at"),
            "status": s.get("status"),
            "version": version,
            "num_episodes": eps_by_session.get(sid, 0),
            "total_cost_usd": round(float(s.get("total_cost_usd") or 0.0), 6),
        }
        v2 = v2_rows.get(sid)
        if v2:
            obj.update({
                "ended_early": bool(v2.get("ended_early")),
                "num_ticks": v2.get("num_ticks"),
                "elapsed_seconds": v2.get("elapsed_seconds"),
            })
        out.append(obj)
    out.sort(key=lambda o: o["invocation_num"], reverse=True)
    return out


def _build_bundle(instance, store: EpisodicStore, invocation_num: int) -> dict | None:
    """Assemble the full session bundle for (instance, invocation_num).

    invocation == session. v1: one episode/session. v2: many tick-episodes share
    the invocation_num (set once per session); the session is keyed by the
    sessions row, episodes by session_id, v2 measurement by the v2_sessions row.
    Returns None if no session has this invocation_num.
    """
    sessions = store.all_sessions()
    session = next(
        (s for s in sessions if s["invocation_num"] == invocation_num), None
    )
    if session is None:
        return None
    sid = session["id"]

    # Episodes for this session, in order.
    episodes = [e for e in store.all_episodes() if e.get("session_id") == sid]
    episodes.sort(key=lambda e: e.get("id") or 0)

    actions = store.actions_for_session(sid)

    # v2 measurement row (absent for v1 / still-running).
    v2 = next(
        (r for r in store.recent_v2_sessions(n=100000) if r["session_id"] == sid),
        None,
    )

    comms = [
        c for c in store.ben_contact_history(limit=100000)
        if c.get("invocation_num") == invocation_num
    ]
    comms.sort(key=lambda c: c.get("id") or 0)

    caps = [
        c for c in store.all_capability_requests()
        if c.get("invocation_num") == invocation_num
    ]
    caps.sort(key=lambda c: c.get("id") or 0)

    subagents = [
        c for c in store.recent_subagent_calls(n=100000)
        if c.get("session_id") == sid
    ]
    subagents.sort(key=lambda c: c.get("id") or 0)

    documents = _collect_documents(instance, actions)
    claude_md_diffs = _collect_claude_md_diffs(store, sid)

    running = session.get("ended_at") is None

    return {
        "instance": instance,
        "invocation_num": invocation_num,
        "session": session,
        "session_id": sid,
        "running": running,
        "episodes": episodes,
        "actions": actions,
        "v2": v2,
        "comms": comms,
        "capabilities": caps,
        "subagents": subagents,
        "documents": documents,
        "claude_md_diffs": claude_md_diffs,
    }


def _collect_documents(instance, actions: list[dict]) -> list[dict]:
    """Derive workspace files written/deleted this session from action args.

    Parses write_file / delete_file actions' args_json for `path`. Last action
    per path wins (so a write-then-delete shows as deleted). Order: first seen.
    """
    order: list[str] = []
    state: dict[str, str] = {}
    for a in actions:
        tool = (a.get("tool_name") or "").lower()
        if tool == "write_file":
            verb = "written"
        elif tool == "delete_file":
            verb = "deleted"
        else:
            continue
        raw = a.get("args_json") or "{}"
        path = None
        try:
            args = json.loads(raw)
            if isinstance(args, dict):
                path = args.get("path")
        except (json.JSONDecodeError, TypeError):
            # args_json truncated mid-blob (write_file content is huge): fall
            # back to a regex over the surviving prefix.
            m = _PATH_RE.search(raw)
            if m:
                try:
                    path = json.loads(f'"{m.group(1)}"')
                except json.JSONDecodeError:
                    path = m.group(1)
        if not path:
            continue
        if path not in state:
            order.append(path)
        state[path] = verb

    docs: list[dict] = []
    for path in order:
        docs.append({"path": path, "action": state[path]})
    return docs


def _collect_claude_md_diffs(store: EpisodicStore, session_id: int) -> list[dict]:
    """Unified diffs for CLAUDE.md snapshots written during this session.

    Each snapshot is diffed against the immediately-preceding snapshot overall
    (prior row by id across all sessions); first-ever snapshot diffs vs empty.
    """
    history = store.claude_md_history(limit=100000)
    history.sort(key=lambda r: r.get("id") or 0)  # chronological

    diffs: list[dict] = []
    for idx, row in enumerate(history):
        if row.get("session_id") != session_id:
            continue
        prev_content = history[idx - 1]["content"] if idx > 0 else ""
        curr_content = row.get("content") or ""
        diff = "".join(
            difflib.unified_diff(
                prev_content.splitlines(keepends=True),
                curr_content.splitlines(keepends=True),
                fromfile="CLAUDE.md (prev)",
                tofile="CLAUDE.md (this snapshot)",
            )
        )
        diffs.append({
            "id": row.get("id"),
            "timestamp": row.get("timestamp"),
            "size": row.get("size"),
            "diff": diff,
        })
    return diffs


def _file_url(instance_id: str, relpath: str) -> str:
    base = request.host_url.rstrip("/")
    return f"{base}/api/instance/{instance_id}/file/{relpath}"


# ---------- JSON serialization of the bundle ----------

def _bundle_json(bundle: dict, verbose: bool) -> dict:
    inst = bundle["instance"]
    s = bundle["session"]
    v2 = bundle["v2"]
    entry = _registry_entry(inst.id)
    trunc = (lambda x: x) if verbose else (lambda x: _trunc(x, _DEFAULT_TRUNC))
    body_trunc = (lambda x: x) if verbose else (lambda x: _trunc(x, _BODY_TRUNC))

    header = {
        "instance_id": inst.id,
        "instance_name": inst.name,
        "version": inst.version,
        "status": s.get("status"),
        "registry_status": entry.get("status"),
        "invocation_num": bundle["invocation_num"],
        "session_id": bundle["session_id"],
        "running": bundle["running"],
        "started_at": s.get("started_at"),
        "ended_at": s.get("ended_at"),
        "wall_clock": _duration_str(s.get("started_at"), s.get("ended_at")),
        "total_cost_usd": round(float(s.get("total_cost_usd") or 0.0), 6),
        "total_tool_calls": s.get("total_tool_calls"),
        "end_reason": s.get("end_reason"),
    }
    if v2:
        header["v2"] = {
            "ended_early": bool(v2.get("ended_early")),
            "elapsed_seconds": v2.get("elapsed_seconds"),
            "min_wake_seconds": v2.get("min_wake_seconds"),
            "awake_minutes": round((v2.get("elapsed_seconds") or 0) / 60.0, 1),
            "reference_minutes": round((v2.get("min_wake_seconds") or 0) / 60.0, 1),
            "num_ticks": v2.get("num_ticks"),
            "decayed_count": v2.get("decayed_count"),
            "consolidated_count": v2.get("consolidated_count"),
            "next_invoke_minutes": v2.get("next_invoke_minutes"),
        }

    narrative = []
    for i, e in enumerate(bundle["episodes"]):
        narrative.append({
            "tick": i,
            "timestamp": e.get("timestamp"),
            "current_focus": e.get("current_focus"),
            "internal_state": e.get("internal_state"),
            "decisions_made": e.get("decisions_made"),
            "journal_entry": e.get("journal_entry"),
            "consolidated": bool(e.get("consolidated")),
            "cost_usd": round(float(e.get("cost_usd") or 0.0), 6),
        })

    actions = [{
        "tool": a.get("tool_name"),
        "args": trunc(a.get("args_json")),
        "result": trunc(a.get("result_summary")),
        "error": a.get("error"),
        "duration_ms": a.get("duration_ms"),
    } for a in bundle["actions"]]

    documents = []
    for d in bundle["documents"]:
        url = _file_url(inst.id, d["path"])
        doc = {"path": d["path"], "action": d["action"], "url": url}
        content, size, inlined = _doc_inline(inst, d["path"], verbose)
        doc["size_bytes"] = size
        doc["inlined"] = inlined
        if inlined:
            doc["content"] = content
        documents.append(doc)

    comms = [{
        "direction": c.get("direction"),
        "channel": c.get("channel"),
        "body": body_trunc(c.get("body")),
    } for c in bundle["comms"]]

    caps = [{
        "capability": c.get("capability"),
        "rationale": c.get("rationale"),
        "status": c.get("status"),
        "ben_response": c.get("ben_response"),
    } for c in bundle["capabilities"]]

    subagents = [{
        "model": c.get("model"),
        "prompt_preview": _trunc(c.get("prompt"), 200),
        "tokens_in": c.get("tokens_in"),
        "tokens_out": c.get("tokens_out"),
        "cost_usd": c.get("cost_usd"),
    } for c in bundle["subagents"]]

    return {
        "header": header,
        "narrative": narrative,
        "actions": actions,
        "documents": documents,
        "documents_note": "Inlined/linked file content reflects the file's CURRENT state; a later invocation may have overwritten it.",
        "claude_md_diffs": bundle["claude_md_diffs"],
        "comms": comms,
        "capability_requests": caps,
        "subagent_calls": subagents,
    }


def _doc_inline(instance, relpath: str, verbose: bool) -> tuple[str | None, int | None, bool]:
    """Return (content_or_None, size_bytes_or_None, inlined_bool) for a doc.

    Inlines files <= 6 KB by default; all sizes in verbose. Missing/binary files
    are not inlined.
    """
    resolved = _resolve_instance_file(instance, relpath)
    if resolved is None or not resolved.exists() or resolved.is_dir():
        return None, None, False
    try:
        size = resolved.stat().st_size
    except OSError:
        return None, None, False
    if not verbose and size > _DOC_INLINE_MAX_BYTES:
        return None, size, False
    try:
        content = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, size, False
    return content, size, True


# ---------- Markdown serialization of the bundle ----------

def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    def esc(x):
        return str(x if x is not None else "").replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(out)


def _bundle_markdown(bundle: dict, verbose: bool) -> str:
    inst = bundle["instance"]
    s = bundle["session"]
    v2 = bundle["v2"]
    trunc = (lambda x: x) if verbose else (lambda x: _trunc(x, _DEFAULT_TRUNC))
    body_trunc = (lambda x: x) if verbose else (lambda x: _trunc(x, _BODY_TRUNC))
    L: list[str] = []

    # --- Header (lead with the gist) ---
    L.append(f"# {inst.id} · invocation {bundle['invocation_num']} ({inst.version})")
    run = "RUNNING" if bundle["running"] else (s.get("status") or "—")
    L.append("")
    L.append(
        f"**{inst.name}** · session #{bundle['session_id']} · status **{run}** · "
        f"cost ${float(s.get('total_cost_usd') or 0.0):.4f} · "
        f"{s.get('total_tool_calls') or 0} tool calls · "
        f"wall-clock {_duration_str(s.get('started_at'), s.get('ended_at'))}"
    )
    L.append("")
    L.append(f"- started: {s.get('started_at') or '—'}")
    L.append(f"- ended: {s.get('ended_at') or '— (still running)'}")
    if s.get("end_reason"):
        L.append(f"- end_reason: {s.get('end_reason')}")
    if v2:
        awake_min = (v2.get("elapsed_seconds") or 0) / 60.0
        ref_min = (v2.get("min_wake_seconds") or 0) / 60.0
        early = bool(v2.get("ended_early"))
        L.append("")
        L.append("## v2 wakefulness (impulse to leave)")
        L.append(
            f"- **ended_early: {early}** — awake **{awake_min:.1f} min** vs "
            f"reference **{ref_min:.1f} min** "
            f"(delta {awake_min - ref_min:+.1f} min)"
        )
        L.append(
            f"- num_ticks: {v2.get('num_ticks')} · "
            f"decayed: {v2.get('decayed_count')} · "
            f"consolidated: {v2.get('consolidated_count')} · "
            f"next_invoke_minutes: {v2.get('next_invoke_minutes')}"
        )
    elif inst.version == "v2":
        L.append("")
        L.append("_v2 measurement row not yet written (session still running or not finalized)._")

    # --- Narrative ---
    L.append("")
    L.append("## Narrative")
    if not bundle["episodes"]:
        L.append("_No episodes logged yet for this session._")
    for i, e in enumerate(bundle["episodes"]):
        cons = " · consolidated" if e.get("consolidated") else ""
        L.append("")
        L.append(f"### Tick {i} — {e.get('timestamp') or '—'}{cons} (${float(e.get('cost_usd') or 0.0):.4f})")
        if e.get("current_focus"):
            L.append(f"- **focus:** {e.get('current_focus')}")
        if e.get("internal_state"):
            L.append(f"- **internal_state:** {e.get('internal_state')}")
        if e.get("decisions_made"):
            L.append(f"- **decisions:** {e.get('decisions_made')}")
        if e.get("journal_entry"):
            L.append(f"- **journal:** {e.get('journal_entry')}")

    # --- Tool actions ---
    L.append("")
    L.append(f"## Tool actions ({len(bundle['actions'])})")
    if not bundle["actions"]:
        L.append("_No tool calls recorded._")
    else:
        rows = [[
            a.get("tool_name"),
            trunc(a.get("args_json")),
            trunc(a.get("result_summary")),
            a.get("error") or "",
            a.get("duration_ms"),
        ] for a in bundle["actions"]]
        L.append(_md_table(["tool", "args", "result", "error", "ms"], rows))

    # --- Documents ---
    L.append("")
    L.append(f"## Documents ({len(bundle['documents'])})")
    L.append("_Inlined/linked content reflects each file's CURRENT state; a later invocation may have overwritten it._")
    if not bundle["documents"]:
        L.append("")
        L.append("_No workspace files written or deleted this invocation._")
    for d in bundle["documents"]:
        url = _file_url(inst.id, d["path"])
        L.append("")
        L.append(f"### `{d['path']}` — {d['action']}")
        L.append(f"- retrieve: {url}")
        if d["action"] == "deleted":
            L.append("- (deleted this session)")
            continue
        content, size, inlined = _doc_inline(inst, d["path"], verbose)
        if size is not None:
            L.append(f"- size: {size} B")
        if inlined and content is not None:
            lang = ""
            if d["path"].endswith(".md"):
                lang = "markdown"
            L.append("")
            L.append(f"```{lang}")
            L.append(content.rstrip("\n"))
            L.append("```")
        elif size is None:
            L.append("- (file missing now)")
        else:
            L.append(f"- (too large to inline at {size} B; fetch via link or use ?verbose=1)")

    # --- CLAUDE.md changes ---
    L.append("")
    L.append("## CLAUDE.md changes")
    if not bundle["claude_md_diffs"]:
        L.append("_No CLAUDE.md change this invocation._")
    for d in bundle["claude_md_diffs"]:
        L.append("")
        L.append(f"_snapshot #{d['id']} @ {d['timestamp']} ({d['size']} B)_")
        L.append("")
        L.append("```diff")
        L.append((d["diff"] or "(no textual change)").rstrip("\n"))
        L.append("```")

    # --- Comms ---
    L.append("")
    L.append(f"## Comms with Ben ({len(bundle['comms'])})")
    if not bundle["comms"]:
        L.append("_None this invocation._")
    else:
        rows = [[c.get("direction"), c.get("channel"), body_trunc(c.get("body"))]
                for c in bundle["comms"]]
        L.append(_md_table(["dir", "channel", "body"], rows))

    # --- Capability requests ---
    L.append("")
    L.append(f"## Capability requests ({len(bundle['capabilities'])})")
    if not bundle["capabilities"]:
        L.append("_None this invocation._")
    else:
        rows = [[c.get("capability"), trunc(c.get("rationale")),
                 c.get("status"), trunc(c.get("ben_response"))]
                for c in bundle["capabilities"]]
        L.append(_md_table(["capability", "rationale", "status", "ben_response"], rows))

    # --- Sub-agent calls ---
    L.append("")
    L.append(f"## Sub-agent calls ({len(bundle['subagents'])})")
    if not bundle["subagents"]:
        L.append("_None this invocation._")
    else:
        rows = [[c.get("model"), _trunc(c.get("prompt"), 200),
                 c.get("tokens_in"), c.get("tokens_out"), c.get("cost_usd")]
                for c in bundle["subagents"]]
        L.append(_md_table(["model", "prompt_preview", "tok_in", "tok_out", "cost"], rows))

    return "\n".join(L) + "\n"


# ---------- routes ----------

@app.route("/api/instances")
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
            "active": bool(entry.get("active", False)),
            "invocation_count": invocation_count,
        })
    return jsonify(out)


@app.route("/api/instance/<instance_id>/invocations")
def api_invocations(instance_id: str):
    inst, err = _load_instance_or_404(instance_id)
    if err:
        return err
    store = EpisodicStore(inst.episodes_db)
    return jsonify(_instance_invocations(inst, store))


@app.route("/api/instance/<instance_id>/invocation/<int:n>")
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


@app.route("/api/instance/<instance_id>/file/<path:relpath>")
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


@app.route("/api/instance/<instance_id>/session/<int:session_id>/research")
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


@app.route("/api/instance/<instance_id>/prereg")
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


@app.route("/api/instance/<instance_id>/prereg/approve", methods=["POST"])
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


@app.route("/api/instance/<instance_id>/synthesis")
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


def _api_endpoints(base: str) -> list[dict]:
    return [
        {
            "method": "GET", "path": "/api", "description": "This discovery document.",
            "params": {"format": "json|md (default md)"},
            "example": f"{base}/api",
        },
        {
            "method": "GET", "path": "/api/instances",
            "description": "List bots (instances): id, name, version, status, active, invocation_count.",
            "params": {}, "example": f"{base}/api/instances",
        },
        {
            "method": "GET", "path": "/api/instance/<id>/invocations",
            "description": "List a bot's invocations (newest first) for navigation: invocation_num, session_id, timestamps, status, num_episodes, cost, and v2 fields (ended_early, num_ticks, elapsed_seconds).",
            "params": {}, "example": f"{base}/api/instance/v2-environmental/invocations",
        },
        {
            "method": "GET", "path": "/api/instance/<id>/invocation/<n>",
            "description": "Full bundle for one invocation (= one session). Header + v2 wakefulness, per-tick narrative, tool-action table, Documents (files the agent wrote), CLAUDE.md unified diffs, comms, capability requests.",
            "params": {"format": "json|md (default md)", "verbose": "1 = untruncated tool output + all docs inlined"},
            "example": f"{base}/api/instance/v2-environmental/invocation/1",
        },
        {
            "method": "GET", "path": "/api/instance/<id>/file/<path>",
            "description": "Raw current content of a workspace document. Doc links in bundles point here.",
            "params": {}, "example": f"{base}/api/instance/v2-environmental/file/CLAUDE.md",
        },
        {
            "method": "GET", "path": "/api/instance/<id>/session/<session_id>/research",
            "description": "Research panel output for one session: synthesized note (raw_observations/inferences/analysis/promoted_claims/disagreements), per-seat notes, and inter-rater kappa.",
            "params": {}, "example": f"{base}/api/instance/v4-continuous/session/1/research",
        },
        {
            "method": "GET", "path": "/api/instance/<id>/prereg",
            "description": "The formal experiment spec + the panel's operationalized coding scheme and its approval status (pending_approval|approved).",
            "params": {}, "example": f"{base}/api/instance/v4-continuous/prereg",
        },
        {
            "method": "POST", "path": "/api/instance/<id>/prereg/approve",
            "description": "Approve the operationalized coding scheme (the human gate); new per-session notes become 'binding'.",
            "params": {}, "example": f"{base}/api/instance/v4-continuous/prereg/approve",
        },
        {
            "method": "GET", "path": "/api/instance/<id>/synthesis",
            "description": "The rolling cumulative report: per-hypothesis verdicts + certainty (plain language), emergent hypotheses, recommendations, drift/changelog, and the deterministic evidence matrix. Latest by default; ?version=N for a specific version.",
            "params": {"version": "N (optional; default latest)"}, "example": f"{base}/api/instance/v4-continuous/synthesis",
        },
    ]


def _api_discovery_md(base: str) -> str:
    eps = _api_endpoints(base)
    lines = [
        "# Autonomous Agent — Data API",
        "",
        "Read-only HTTP API over an autonomous-agent experiment's history. No auth.",
        "Responses are **Markdown by default** (token-lean, made for LLM reading); append "
        "`?format=json` to any endpoint for structured JSON.",
        "",
        f"Base URL: `{base}`",
        "",
        "## Data model",
        "- **instance** = one bot, with a `version` (`v1` or `v2`). Discover them via `/api/instances`.",
        "- **invocation** = one *session*, keyed by `invocation_num`.",
        "  - v1: one episode per invocation.",
        "  - v2: a session is a continuous **tick loop**, so one invocation contains *many* tick-episodes "
        "sharing the same invocation_num.",
        "- **v2 wakefulness** (the experiment's headline signal): each v2 session logs how long the agent "
        "chose to stay awake vs a reference length — `ended_early`, `elapsed` vs `min_wake` (the \"impulse to leave\").",
        "",
        "## Endpoints",
    ]
    for e in eps:
        lines.append(f"### `{e['method']} {e['path']}`")
        lines.append(e["description"])
        if e["params"]:
            lines.append("params: " + ", ".join(f"`{k}` — {v}" for k, v in e["params"].items()))
        lines.append(f"example: {e['example']}")
        lines.append("")
    lines += [
        "## Suggested flow",
        "1. `GET /api/instances` → pick a bot.",
        "2. `GET /api/instance/<id>/invocations` → see which sessions exist.",
        "3. `GET /api/instance/<id>/invocation/<n>` → read the full session. Add `?verbose=1` for "
        "untruncated tool output or large documents.",
        "",
        f"This document as JSON: {base}/api?format=json",
    ]
    return "\n".join(lines)


@app.route("/api")
@app.route("/api/")
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


@app.route("/llms.txt")
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


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", "8081"))
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=False)
