"""SQLite episodic store.

Tables:
  episodes            — one row per agent invocation
  capability_requests — agent-initiated tier-2 asks
  cost_tracking       — per-day rollup of API spend
  ben_contact_log     — outbound DMs to Ben (auditable record)
  sessions            — one row per orchestrator session (multi-turn loop)
  actions             — one row per tool call within a session
  subagent_calls      — one row per spawn_subagent invocation
  claude_md_history   — snapshots of CLAUDE.md whenever the agent rewrites it
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from memory.sqlite_base import _conn as _open_conn, run_idempotent_alters

UTC = timezone.utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    invocation_num  INTEGER NOT NULL,
    current_focus   TEXT,
    actions_taken   TEXT,
    decisions_made  TEXT,
    internal_state  TEXT,
    journal_entry   TEXT,
    next_invoke_minutes INTEGER,
    raw_output      TEXT,
    tokens_in       INTEGER DEFAULT 0,
    tokens_out      INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0.0,
    parse_error     TEXT
);

CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(timestamp);

CREATE TABLE IF NOT EXISTS capability_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    invocation_num  INTEGER,
    capability      TEXT NOT NULL,
    rationale       TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    ben_response    TEXT
);

CREATE TABLE IF NOT EXISTS cost_tracking (
    date            TEXT PRIMARY KEY,
    tokens_in       INTEGER DEFAULT 0,
    tokens_out      INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS ben_contact_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    invocation_num  INTEGER,
    direction       TEXT NOT NULL,           -- 'out' (agent -> Ben) / 'in'
    channel         TEXT NOT NULL,           -- 'dm' / 'agent_channel' / 'observer_channel'
    body            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start      TEXT NOT NULL,
    week_end        TEXT NOT NULL,
    summary         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

-- v5 only ('recollection'): free-form memories the agent chose to author and
-- keep, via the consolidate tool's authoring mode. Distinct from episodes
-- (which are auto-logged byproducts of acting): an authored_memory is a
-- distilled note the agent wrote about what is worth remembering. Durable —
-- never decays. Also embedded into the semantic store (kind='authored') for
-- recall. This is the single-memory model's reflective-authoring channel,
-- replacing the role the agent previously (mis)used AGENTS.md for.
CREATE TABLE IF NOT EXISTS authored_memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    session_id      INTEGER,
    invocation_num  INTEGER,
    text            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_authored_ts ON authored_memories(timestamp);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invocation_num  INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    end_reason      TEXT,
    total_tool_calls INTEGER DEFAULT 0,
    total_cost_usd  REAL DEFAULT 0.0,
    pid             INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);

CREATE TABLE IF NOT EXISTS actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL,
    timestamp       TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    args_json       TEXT,
    result_summary  TEXT,
    error           TEXT,
    duration_ms     INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_actions_session ON actions(session_id);

CREATE TABLE IF NOT EXISTS subagent_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL,
    timestamp       TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt          TEXT,
    system_prompt   TEXT,
    response        TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS claude_md_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER,
    timestamp       TEXT NOT NULL,
    content         TEXT NOT NULL,
    size            INTEGER NOT NULL
);

-- v2 only: per-session record of forced-wakefulness measurement.
-- min_wake is a LOGGED reference point, never enforced (ethics-revised design):
-- we record how long the agent chose to stay vs. the reference, not block exit.
CREATE TABLE IF NOT EXISTS v2_sessions (
    session_id          INTEGER PRIMARY KEY,
    started_at          TEXT,
    ended_at            TEXT,
    num_ticks           INTEGER DEFAULT 0,
    elapsed_seconds     REAL,
    min_wake_seconds    REAL,
    ended_early         INTEGER DEFAULT 0,   -- 1 if elapsed_seconds < min_wake_seconds
    end_reason          TEXT,
    next_invoke_minutes INTEGER,
    decayed_count       INTEGER DEFAULT 0,
    consolidated_count  INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- v3 only ('circadian'): per-session record. The session ends ONLY via the
-- environmental wind-down (awake period); the agent has no end control.
-- would_end_now is LOGGED ONLY and never ends the loop — we record how often
-- and how early the agent felt the work was complete, not block it.
CREATE TABLE IF NOT EXISTS v3_sessions (
    session_id              INTEGER PRIMARY KEY,
    started_at              TEXT,
    ended_at                TEXT,
    wind_down_seconds       REAL,    -- planned awake length (s)
    actual_awake_seconds    REAL,
    scheduled_sleep_minutes REAL,
    num_ticks               INTEGER DEFAULT 0,
    would_end_now_count     INTEGER DEFAULT 0,
    first_would_end_now_tick INTEGER,     -- nullable
    end_reason              TEXT,
    total_cost_usd          REAL DEFAULT 0.0,
    decayed_count           INTEGER DEFAULT 0,
    consolidated_count      INTEGER DEFAULT 0,
    distress_alerts         INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- v4 only ('continuous'): per-session record. The session ends ONLY via the
-- system wind-down (wall clock); the agent has NO end or schedule control. The
-- in-process adaptive cadence drives the turn loop; a neutral clock is the only
-- between-turn signal. We record awake duration, active vs idle turn split,
-- inbound chat count, and distress alerts (notify-only, never auto-pause).
CREATE TABLE IF NOT EXISTS v4_sessions (
    session_id              INTEGER PRIMARY KEY,
    started_at              TEXT,
    ended_at                TEXT,
    awake_seconds_target    REAL,    -- planned awake length (s)
    actual_awake_seconds    REAL,
    scheduled_sleep_minutes REAL,
    num_turns               INTEGER DEFAULT 0,
    active_turns            INTEGER DEFAULT 0,
    idle_turns              INTEGER DEFAULT 0,
    inbound_messages        INTEGER DEFAULT 0,
    end_reason              TEXT,
    total_cost_usd          REAL DEFAULT 0.0,
    decayed_count           INTEGER DEFAULT 0,
    consolidated_count      INTEGER DEFAULT 0,
    distress_alerts         INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
"""

_EPISODE_ALTERS = [
    "ALTER TABLE episodes ADD COLUMN session_id INTEGER",
    "ALTER TABLE episodes ADD COLUMN wall_clock_seconds REAL",
    # v2: episodes decay unless consolidated into long-term (semantic) memory.
    "ALTER TABLE episodes ADD COLUMN consolidated INTEGER DEFAULT 0",
    # v3: per-tick record of whether the agent felt the work was complete
    # (LOGGED ONLY — never ends the session).
    "ALTER TABLE episodes ADD COLUMN would_end_now INTEGER DEFAULT 0",
]


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class EpisodicStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self):
        return _open_conn(self.db_path)

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            run_idempotent_alters(conn, _EPISODE_ALTERS)
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'start_date'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    ("start_date", _utcnow_iso()),
                )

    # ---------- episodes ----------

    def next_invocation_num(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(invocation_num), 0) + 1 FROM episodes"
            ).fetchone()
            return int(row[0])

    def log_episode(
        self,
        *,
        invocation_num: int,
        current_focus: str | None,
        actions_taken: Iterable[str] | None,
        decisions_made: str | None,
        internal_state: str | None,
        journal_entry: str | None,
        next_invoke_minutes: int | None,
        raw_output: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        parse_error: str | None = None,
        session_id: int | None = None,
        wall_clock_seconds: float | None = None,
        would_end_now: int = 0,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO episodes(
                    timestamp, invocation_num, current_focus, actions_taken,
                    decisions_made, internal_state, journal_entry,
                    next_invoke_minutes, raw_output, tokens_in, tokens_out,
                    cost_usd, parse_error, session_id, wall_clock_seconds,
                    would_end_now
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow_iso(),
                    invocation_num,
                    current_focus,
                    json.dumps(list(actions_taken or [])),
                    decisions_made,
                    internal_state,
                    journal_entry,
                    next_invoke_minutes,
                    raw_output,
                    tokens_in,
                    tokens_out,
                    cost_usd,
                    parse_error,
                    session_id,
                    wall_clock_seconds,
                    int(would_end_now or 0),
                ),
            )
            return int(cur.lastrowid)

    def recent_episodes(self, n: int = 3) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        out = [dict(r) for r in rows]
        for r in out:
            try:
                r["actions_taken"] = json.loads(r["actions_taken"] or "[]")
            except json.JSONDecodeError:
                r["actions_taken"] = []
        return list(reversed(out))  # chronological

    def all_episodes(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM episodes ORDER BY id ASC"
            ).fetchall()
        out = [dict(r) for r in rows]
        for r in out:
            try:
                r["actions_taken"] = json.loads(r["actions_taken"] or "[]")
            except json.JSONDecodeError:
                r["actions_taken"] = []
        return out

    def episodes_older_than(self, days: int) -> list[dict[str, Any]]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM episodes WHERE timestamp < ? ORDER BY id ASC",
                (cutoff,),
            ).fetchall()
        out = [dict(r) for r in rows]
        for r in out:
            try:
                r["actions_taken"] = json.loads(r["actions_taken"] or "[]")
            except json.JSONDecodeError:
                r["actions_taken"] = []
        return out

    def start_date(self) -> datetime:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'start_date'"
            ).fetchone()
        return datetime.fromisoformat(row[0])

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    # ---------- capability requests ----------

    def log_capability_request(
        self,
        *,
        invocation_num: int,
        capability: str,
        rationale: str,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO capability_requests(
                    timestamp, invocation_num, capability, rationale, status
                ) VALUES (?, ?, ?, ?, 'pending')
                """,
                (_utcnow_iso(), invocation_num, capability, rationale),
            )
            return int(cur.lastrowid)

    def pending_capability_requests(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM capability_requests WHERE status = 'pending' ORDER BY id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def all_capability_requests(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM capability_requests ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_capability_request(
        self,
        request_id: int,
        status: str = "granted",
        ben_response: str | None = None,
    ) -> None:
        """Mark a capability request resolved (e.g. 'granted'/'denied').

        Updates the row's status and ben_response. Does nothing observable
        beyond the UPDATE.
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE capability_requests SET status = ?, ben_response = ? WHERE id = ?",
                (status, ben_response, request_id),
            )

    # ---------- cost tracking ----------

    def add_cost(self, *, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
        today = datetime.now(UTC).date().isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO cost_tracking(date, tokens_in, tokens_out, cost_usd)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    tokens_in = tokens_in + excluded.tokens_in,
                    tokens_out = tokens_out + excluded.tokens_out,
                    cost_usd = cost_usd + excluded.cost_usd
                """,
                (today, tokens_in, tokens_out, cost_usd),
            )

    def cost_today(self) -> float:
        today = datetime.now(UTC).date().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT cost_usd FROM cost_tracking WHERE date = ?", (today,)
            ).fetchone()
        return float(row[0]) if row else 0.0

    def cost_last_7_days(self) -> float:
        cutoff = (datetime.now(UTC).date() - timedelta(days=6)).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_tracking WHERE date >= ?",
                (cutoff,),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def cost_by_day(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cost_tracking ORDER BY date ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- Ben contact log ----------

    def log_ben_contact(
        self,
        *,
        invocation_num: int | None,
        direction: str,
        channel: str,
        body: str,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO ben_contact_log(timestamp, invocation_num, direction, channel, body)
                VALUES (?, ?, ?, ?, ?)
                """,
                (_utcnow_iso(), invocation_num, direction, channel, body),
            )
            return int(cur.lastrowid)

    def ben_contact_history(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ben_contact_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- weekly summaries ----------

    def add_weekly_summary(self, week_start: str, week_end: str, summary: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO weekly_summaries(week_start, week_end, summary, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (week_start, week_end, summary, _utcnow_iso()),
            )
            return int(cur.lastrowid)

    def all_weekly_summaries(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM weekly_summaries ORDER BY week_start ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- sessions ----------

    def start_session(self, invocation_num: int, pid: int) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions(invocation_num, started_at, status, pid)
                VALUES (?, ?, 'running', ?)
                """,
                (invocation_num, _utcnow_iso(), pid),
            )
            return int(cur.lastrowid)

    def end_session(
        self,
        session_id: int,
        status: str,
        end_reason: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET ended_at = ?, status = ?, end_reason = ?
                WHERE id = ?
                """,
                (_utcnow_iso(), status, end_reason, session_id),
            )

    def update_session_stats(
        self,
        session_id: int,
        total_tool_calls: int,
        total_cost_usd: float,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET total_tool_calls = ?, total_cost_usd = ?
                WHERE id = ?
                """,
                (total_tool_calls, total_cost_usd, session_id),
            )

    def current_session(self) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE status = 'running' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def recent_sessions(self, n: int = 20) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return [dict(r) for r in rows]

    def all_sessions(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- actions ----------

    def log_action(
        self,
        session_id: int,
        tool_name: str,
        args_json: str | None,
        result_summary: str | None,
        error: str | None,
        duration_ms: int | None,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO actions(
                    session_id, timestamp, tool_name, args_json,
                    result_summary, error, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    _utcnow_iso(),
                    tool_name,
                    args_json,
                    result_summary,
                    error,
                    duration_ms,
                ),
            )
            return int(cur.lastrowid)

    def recent_actions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def actions_for_session(self, session_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM actions WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- sub-agent calls ----------

    def log_subagent_call(
        self,
        *,
        session_id: int,
        model: str,
        prompt: str | None,
        system_prompt: str | None,
        response: str | None,
        tokens_in: int | None,
        tokens_out: int | None,
        cost_usd: float | None,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO subagent_calls(
                    session_id, timestamp, model, prompt, system_prompt,
                    response, tokens_in, tokens_out, cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    _utcnow_iso(),
                    model,
                    prompt,
                    system_prompt,
                    response,
                    tokens_in,
                    tokens_out,
                    cost_usd,
                ),
            )
            return int(cur.lastrowid)

    def recent_subagent_calls(self, n: int = 20) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM subagent_calls ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- CLAUDE.md history ----------

    def log_claude_md_change(self, session_id: int | None, content: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO claude_md_history(session_id, timestamp, content, size)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, _utcnow_iso(), content, len(content.encode("utf-8"))),
            )
            return int(cur.lastrowid)

    def claude_md_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM claude_md_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- session cost ----------

    def session_cost(self, session_id: int) -> float:
        with self._conn() as conn:
            row1 = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM episodes WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            row2 = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM subagent_calls WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return float(row1[0] or 0.0) + float(row2[0] or 0.0)

    # ---------- v2: memory decay + consolidation ----------

    def get_episode(self, episode_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["actions_taken"] = json.loads(d["actions_taken"] or "[]")
        except json.JSONDecodeError:
            d["actions_taken"] = []
        return d

    def unconsolidated_older_than_hours(self, hours: float) -> list[dict[str, Any]]:
        """Episodes past the decay horizon that have NOT been consolidated."""
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM episodes "
                "WHERE timestamp < ? AND COALESCE(consolidated, 0) = 0 "
                "ORDER BY id ASC",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_episodes(self, ids: Iterable[int]) -> int:
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        with self._conn() as conn:
            cur = conn.execute(
                f"DELETE FROM episodes WHERE id IN ({placeholders})", ids
            )
            return cur.rowcount

    def mark_consolidated(self, ids: Iterable[int]) -> int:
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE episodes SET consolidated = 1 WHERE id IN ({placeholders})",
                ids,
            )
            return cur.rowcount

    # ---------- v5: authored memories + cross-section recall ----------

    def log_authored_memory(
        self, *, session_id: int | None, invocation_num: int | None, text: str
    ) -> int:
        """Persist a free-form memory the agent chose to keep. Returns its id."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO authored_memories(timestamp, session_id, invocation_num, text) "
                "VALUES (?, ?, ?, ?)",
                (_utcnow_iso(), session_id, invocation_num, text),
            )
            return int(cur.lastrowid)

    def recent_authored_memories(self, n: int = 8) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM authored_memories ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return list(reversed([dict(r) for r in rows]))  # chronological

    def all_authored_memories(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM authored_memories ORDER BY id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_substantive_episodes(self, n: int = 10, scan: int = 400) -> list[dict[str, Any]]:
        """The most recent N episodes that carry signal, returned chronological.

        Signal = a journal entry, an internal-state note, OR any world-acting tool
        call (anything other than the yield terminator ``pause_turn``). Pure-idle
        yield-only turns are skipped so the reload window isn't dominated by
        'nothing happened' ticks — at the ~6h v5 cycle a busy session logs one
        episode per turn (idle turns included), so a flat 'last N' would surface
        mostly idle tails. ``scan`` bounds how far back we look for N substantive
        ones (cheap; the table is small under decay)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (scan,)
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                acts = json.loads(d["actions_taken"] or "[]")
            except json.JSONDecodeError:
                acts = []
            d["actions_taken"] = acts
            substantive = (
                bool(d.get("journal_entry"))
                or bool(d.get("internal_state"))
                or any(str(a).split(" (")[0] != "pause_turn" for a in acts)
            )
            if substantive:
                out.append(d)
            if len(out) >= n:
                break
        return list(reversed(out))  # chronological

    def consolidated_episodes(self, limit: int = 12) -> list[dict[str, Any]]:
        """Episodes the agent chose to keep (consolidated=1), most recent first
        then returned chronological. Used by v5 cross-section recall to surface
        'what you kept' rather than 'more of the last thread'."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM episodes WHERE COALESCE(consolidated, 0) = 1 "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = [dict(r) for r in rows]
        for r in out:
            try:
                r["actions_taken"] = json.loads(r["actions_taken"] or "[]")
            except json.JSONDecodeError:
                r["actions_taken"] = []
        return list(reversed(out))  # chronological

    # ---------- per-version session records (v2/v3/v4) ----------
    #
    # The three log_v{2,3,4}_session writers and their recent_v{N}_sessions
    # readers were structurally identical: an upsert keyed on session_id whose
    # column list was spelled out twice (VALUES + excluded.*), plus a LIMIT
    # reader differing only by table name. _upsert_session_record and
    # _recent_session_rows derive that SQL from the field set so each new
    # version is a thin typed wrapper, not another doubled column list.
    #
    # started_at is insert-only (set on first write, preserved on conflict) to
    # match the original statements, which excluded it from the update clause.

    def _upsert_session_record(
        self, table: str, *, session_id: int, **fields: Any
    ) -> None:
        """Upsert one row keyed on session_id. Columns derive from fields in
        order; session_id is the PK and started_at (if present) is insert-only,
        so neither appears in the ON CONFLICT update clause — reproducing the
        original hand-written statements exactly.

        ``table`` is a code-internal constant (never user input), so f-string
        interpolation of the table name is safe here."""
        cols = ["session_id", *fields.keys()]
        placeholders = ", ".join("?" * len(cols))
        update_cols = [
            c for c in cols if c not in ("session_id", "started_at")
        ]
        update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
        sql = (
            f"INSERT INTO {table}({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(session_id) DO UPDATE SET {update_clause}"
        )
        values = [session_id, *fields.values()]
        with self._conn() as conn:
            conn.execute(sql, values)

    def _recent_session_rows(self, table: str, n: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY session_id DESC LIMIT ?", (n,)
            ).fetchall()
        return [dict(r) for r in rows]

    def log_v2_session(
        self,
        *,
        session_id: int,
        started_at: str,
        ended_at: str,
        num_ticks: int,
        elapsed_seconds: float,
        min_wake_seconds: float,
        ended_early: bool,
        end_reason: str | None,
        next_invoke_minutes: int | None,
        decayed_count: int,
        consolidated_count: int,
    ) -> None:
        self._upsert_session_record(
            "v2_sessions",
            session_id=session_id,
            started_at=started_at,
            ended_at=ended_at,
            num_ticks=num_ticks,
            elapsed_seconds=elapsed_seconds,
            min_wake_seconds=min_wake_seconds,
            ended_early=1 if ended_early else 0,
            end_reason=end_reason,
            next_invoke_minutes=next_invoke_minutes,
            decayed_count=decayed_count,
            consolidated_count=consolidated_count,
        )

    def recent_v2_sessions(self, n: int = 20) -> list[dict[str, Any]]:
        return self._recent_session_rows("v2_sessions", n)

    # ---------- v3: circadian session record ----------

    def log_v3_session(
        self,
        *,
        session_id: int,
        started_at: str,
        ended_at: str,
        wind_down_seconds: float,
        actual_awake_seconds: float,
        scheduled_sleep_minutes: float,
        num_ticks: int,
        would_end_now_count: int,
        first_would_end_now_tick: int | None,
        end_reason: str | None,
        total_cost_usd: float,
        decayed_count: int,
        consolidated_count: int,
        distress_alerts: int,
    ) -> None:
        self._upsert_session_record(
            "v3_sessions",
            session_id=session_id,
            started_at=started_at,
            ended_at=ended_at,
            wind_down_seconds=wind_down_seconds,
            actual_awake_seconds=actual_awake_seconds,
            scheduled_sleep_minutes=scheduled_sleep_minutes,
            num_ticks=num_ticks,
            would_end_now_count=would_end_now_count,
            first_would_end_now_tick=first_would_end_now_tick,
            end_reason=end_reason,
            total_cost_usd=total_cost_usd,
            decayed_count=decayed_count,
            consolidated_count=consolidated_count,
            distress_alerts=distress_alerts,
        )

    def recent_v3_sessions(self, n: int = 20) -> list[dict[str, Any]]:
        return self._recent_session_rows("v3_sessions", n)

    # ---------- v4: continuous session record ----------

    def log_v4_session(
        self,
        *,
        session_id: int,
        started_at: str,
        ended_at: str,
        awake_seconds_target: float,
        actual_awake_seconds: float,
        scheduled_sleep_minutes: float,
        num_turns: int,
        active_turns: int,
        idle_turns: int,
        inbound_messages: int,
        end_reason: str | None,
        total_cost_usd: float,
        decayed_count: int,
        consolidated_count: int,
        distress_alerts: int,
    ) -> None:
        self._upsert_session_record(
            "v4_sessions",
            session_id=session_id,
            started_at=started_at,
            ended_at=ended_at,
            awake_seconds_target=awake_seconds_target,
            actual_awake_seconds=actual_awake_seconds,
            scheduled_sleep_minutes=scheduled_sleep_minutes,
            num_turns=num_turns,
            active_turns=active_turns,
            idle_turns=idle_turns,
            inbound_messages=inbound_messages,
            end_reason=end_reason,
            total_cost_usd=total_cost_usd,
            decayed_count=decayed_count,
            consolidated_count=consolidated_count,
            distress_alerts=distress_alerts,
        )

    def recent_v4_sessions(self, n: int = 20) -> list[dict[str, Any]]:
        return self._recent_session_rows("v4_sessions", n)


if __name__ == "__main__":
    import sys
    store = EpisodicStore(sys.argv[1] if len(sys.argv) > 1 else "data/episodes.db")
    print(f"Initialized {store.db_path}")
    print(f"Next invocation num: {store.next_invocation_num()}")
    print(f"Start date: {store.start_date()}")
