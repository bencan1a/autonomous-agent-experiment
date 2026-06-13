"""SQLite store for research-panel artifacts.

Opens the SAME ``instances/<id>/data/episodes.db`` as the episodic store (single
file keeps the dashboard simple) but owns its own ``CREATE TABLE IF NOT EXISTS``
schema — keeping ``memory/episodic.py`` (the subject's memory surface) focused.
Mirrors that module's ``sqlite3`` + ``row_factory`` + ``@contextmanager``
conventions. Schema is created idempotently on init, so no migration step.

Tables (phase 1, built now):
  research_prereg      — one operationalized coding scheme per (experiment, spec_hash)
  research_seat_notes  — one row per seat per session (independent review round)
  research_notes       — one synthesized note per session (the headline)
  research_kappa       — inter-rater agreement rows (health metric)

Seam tables (phase 2/3, created now, written later):
  research_cumulative  — living synthesis report across sessions
  research_proposals   — experiment-improvement proposals
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc

RESEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_prereg (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id   TEXT NOT NULL,
    spec_hash       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending_approval',  -- pending_approval | approved
    approved_at     TEXT,
    spec_source     TEXT,                       -- path the spec was read from
    hypotheses_json TEXT,                       -- copied verbatim from the spec
    ivars_json      TEXT,
    dvars_json      TEXT,
    controls_json   TEXT,
    success_json    TEXT,
    stopping_json   TEXT,
    code_vocab_json TEXT,                        -- LLM-operationalized coding scheme
    raw_output      TEXT,
    model           TEXT,
    tokens_in       INTEGER DEFAULT 0,
    tokens_out      INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0.0,
    UNIQUE (experiment_id, spec_hash)
);

CREATE TABLE IF NOT EXISTS research_seat_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL,
    invocation_num  INTEGER,
    created_at      TEXT NOT NULL,
    seat_id         TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    school          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ok',   -- ok | failed | parse_error
    note_json       TEXT,
    raw_output      TEXT,
    parse_error     TEXT,
    tokens_in       INTEGER DEFAULT 0,
    tokens_out      INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0.0,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_research_seat_notes_session ON research_seat_notes(session_id);

CREATE TABLE IF NOT EXISTS research_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL UNIQUE,
    invocation_num  INTEGER,
    created_at      TEXT NOT NULL,
    coding_status   TEXT NOT NULL DEFAULT 'provisional',  -- provisional | binding
    debate_rounds   INTEGER DEFAULT 1,
    note_json       TEXT,
    raw_output      TEXT,
    parse_error     TEXT,
    degraded        INTEGER DEFAULT 0,
    embedded        INTEGER DEFAULT 0,
    total_cost_usd  REAL DEFAULT 0.0,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS research_kappa (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    metric          TEXT NOT NULL,              -- fleiss | cohen
    seat_a          TEXT,
    seat_b          TEXT,
    value           REAL,
    n_codes         INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_research_kappa_session ON research_kappa(session_id);

-- SEAM (phase 2): cumulative living report. rederived_from + up_to_session_id encode
-- the anti-drift discipline (re-derive from raw logs/notes, not the prior summary).
CREATE TABLE IF NOT EXISTS research_cumulative (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    version         INTEGER NOT NULL,
    rederived_from  TEXT,                       -- 'raw_logs' | 'prior_summary'
    up_to_session_id INTEGER,
    report_json     TEXT,
    raw_output      TEXT,
    cost_usd        REAL DEFAULT 0.0
);

-- Operator-response advisories (when the subject reaches out to the operator).
CREATE TABLE IF NOT EXISTS research_advisories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ben_contact_id  INTEGER NOT NULL UNIQUE,   -- the ben_contact_log row advised on
    session_id      INTEGER,
    invocation_num  INTEGER,
    created_at      TEXT NOT NULL,
    agent_message   TEXT,
    advisory_json   TEXT,
    raw_output      TEXT,
    posted          INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_research_advisories_contact ON research_advisories(ben_contact_id);

-- SEAM (phase 3): improvement proposals.
CREATE TABLE IF NOT EXISTS research_proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    session_id      INTEGER,
    proposal_json   TEXT,
    status          TEXT NOT NULL DEFAULT 'open',   -- open | accepted | rejected
    raw_output      TEXT
);
"""

# Idempotent column additions (Phase 2 cumulative report). Mirrors episodic._EPISODE_ALTERS:
# tolerate "duplicate column" so re-running on an existing DB is a no-op.
_RESEARCH_ALTERS = [
    "ALTER TABLE research_cumulative ADD COLUMN spec_hash TEXT",
    "ALTER TABLE research_cumulative ADD COLUMN up_to_session_count INTEGER DEFAULT 0",
    "ALTER TABLE research_cumulative ADD COLUMN drift_flagged INTEGER DEFAULT 0",
    "ALTER TABLE research_cumulative ADD COLUMN degraded INTEGER DEFAULT 0",
]


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _loads(val: Any) -> Any:
    if val is None:
        return None
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None


class ResearchStore:
    """Research-panel persistence over the instance's episodes.db."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(RESEARCH_SCHEMA)
            for stmt in _RESEARCH_ALTERS:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- prereg / coding scheme ----------

    def get_prereg(self, experiment_id: str, spec_hash: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM research_prereg WHERE experiment_id = ? AND spec_hash = ?",
                (experiment_id, spec_hash),
            ).fetchone()
        return self._prereg_row(row)

    def latest_prereg(self, experiment_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM research_prereg WHERE experiment_id = ? ORDER BY id DESC LIMIT 1",
                (experiment_id,),
            ).fetchone()
        return self._prereg_row(row)

    @staticmethod
    def _prereg_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        for k in (
            "hypotheses_json", "ivars_json", "dvars_json", "controls_json",
            "success_json", "stopping_json", "code_vocab_json",
        ):
            d[k.removesuffix("_json")] = _loads(d.get(k))
        return d

    def add_prereg(
        self,
        *,
        experiment_id: str,
        spec_hash: str,
        spec_source: str | None,
        hypotheses: Any,
        ivars: Any,
        dvars: Any,
        controls: Any,
        success: Any,
        stopping: Any,
        code_vocab: Any,
        raw_output: str | None,
        model: str | None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO research_prereg(
                    experiment_id, spec_hash, created_at, status, spec_source,
                    hypotheses_json, ivars_json, dvars_json, controls_json,
                    success_json, stopping_json, code_vocab_json, raw_output,
                    model, tokens_in, tokens_out, cost_usd
                ) VALUES (?, ?, ?, 'pending_approval', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(experiment_id, spec_hash) DO UPDATE SET
                    code_vocab_json = excluded.code_vocab_json,
                    raw_output = excluded.raw_output
                """,
                (
                    experiment_id, spec_hash, _utcnow_iso(), spec_source,
                    json.dumps(hypotheses), json.dumps(ivars), json.dumps(dvars),
                    json.dumps(controls), json.dumps(success), json.dumps(stopping),
                    json.dumps(code_vocab), raw_output, model,
                    tokens_in, tokens_out, cost_usd,
                ),
            )
            rid = cur.lastrowid
            if not rid:
                row = conn.execute(
                    "SELECT id FROM research_prereg WHERE experiment_id = ? AND spec_hash = ?",
                    (experiment_id, spec_hash),
                ).fetchone()
                rid = row[0] if row else 0
        return int(rid)

    def approve_prereg(self, experiment_id: str, spec_hash: str | None = None) -> bool:
        """Approve the coding scheme (the human gate). Returns True if a row was flipped."""
        with self._conn() as conn:
            if spec_hash is None:
                row = conn.execute(
                    "SELECT spec_hash FROM research_prereg WHERE experiment_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (experiment_id,),
                ).fetchone()
                if row is None:
                    return False
                spec_hash = row[0]
            cur = conn.execute(
                "UPDATE research_prereg SET status = 'approved', approved_at = ? "
                "WHERE experiment_id = ? AND spec_hash = ?",
                (_utcnow_iso(), experiment_id, spec_hash),
            )
            return cur.rowcount > 0

    # ---------- seat notes ----------

    def add_seat_note(
        self,
        *,
        session_id: int,
        invocation_num: int | None,
        seat_id: str,
        provider: str,
        model: str,
        school: str,
        status: str,
        note: Any = None,
        raw_output: str | None = None,
        parse_error: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO research_seat_notes(
                    session_id, invocation_num, created_at, seat_id, provider,
                    model, school, status, note_json, raw_output, parse_error,
                    tokens_in, tokens_out, cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, invocation_num, _utcnow_iso(), seat_id, provider,
                    model, school, status,
                    json.dumps(note) if note is not None else None,
                    raw_output, parse_error, tokens_in, tokens_out, cost_usd,
                ),
            )
            return int(cur.lastrowid)

    def seat_notes_for_session(self, session_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM research_seat_notes WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["note"] = _loads(d.get("note_json"))
            out.append(d)
        return out

    # ---------- synthesized note ----------

    def add_synth_note(
        self,
        *,
        session_id: int,
        invocation_num: int | None,
        coding_status: str,
        debate_rounds: int,
        note: Any = None,
        raw_output: str | None = None,
        parse_error: str | None = None,
        degraded: bool = False,
        total_cost_usd: float = 0.0,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO research_notes(
                    session_id, invocation_num, created_at, coding_status,
                    debate_rounds, note_json, raw_output, parse_error, degraded,
                    embedded, total_cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    coding_status = excluded.coding_status,
                    debate_rounds = excluded.debate_rounds,
                    note_json = excluded.note_json,
                    raw_output = excluded.raw_output,
                    parse_error = excluded.parse_error,
                    degraded = excluded.degraded,
                    total_cost_usd = excluded.total_cost_usd
                """,
                (
                    session_id, invocation_num, _utcnow_iso(), coding_status,
                    debate_rounds,
                    json.dumps(note) if note is not None else None,
                    raw_output, parse_error, int(bool(degraded)), total_cost_usd,
                ),
            )
            rid = cur.lastrowid
            if not rid:
                row = conn.execute(
                    "SELECT id FROM research_notes WHERE session_id = ?", (session_id,)
                ).fetchone()
                rid = row[0] if row else 0
        return int(rid)

    def note_for_session(self, session_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM research_notes WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["note"] = _loads(d.get("note_json"))
        return d

    def note_for_invocation(self, invocation_num: int) -> dict[str, Any] | None:
        """The research note for an invocation, keyed directly by invocation_num.

        Preferred over session-id mapping for display: an invocation can span more
        than one session row (e.g. a killed session that logged no episodes left
        the invocation number free for the retry), and the note lives on whichever
        session actually ran. Returns the most recent note if several exist."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM research_notes WHERE invocation_num = ? "
                "ORDER BY session_id DESC LIMIT 1",
                (invocation_num,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["note"] = _loads(d.get("note_json"))
        return d

    def mark_note_embedded(self, note_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE research_notes SET embedded = 1 WHERE id = ?", (note_id,)
            )

    def recent_notes(self, n: int = 20) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM research_notes ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["note"] = _loads(d.get("note_json"))
            out.append(d)
        return out

    # ---------- kappa ----------

    def add_kappa(
        self,
        *,
        session_id: int,
        metric: str,
        value: float | None,
        n_codes: int,
        seat_a: str | None = None,
        seat_b: str | None = None,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO research_kappa(
                    session_id, created_at, metric, seat_a, seat_b, value, n_codes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, _utcnow_iso(), metric, seat_a, seat_b, value, n_codes),
            )
            return int(cur.lastrowid)

    def kappa_for_session(self, session_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM research_kappa WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_session_artifacts(self, session_id: int) -> None:
        """Drop prior seat notes + kappa for a session so a re-run is idempotent.

        The synthesized note row upserts on session_id and is left to be replaced.
        """
        with self._conn() as conn:
            conn.execute("DELETE FROM research_seat_notes WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM research_kappa WHERE session_id = ?", (session_id,))

    # ---------- cumulative living report (Phase 2) ----------

    def next_cumulative_version(self, experiment_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM research_cumulative WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        return int(row[0])

    def add_cumulative(
        self,
        *,
        experiment_id: str,
        spec_hash: str | None,
        report: Any,
        rederived_from: str = "raw_notes",
        up_to_session_id: int | None = None,
        up_to_session_count: int = 0,
        drift_flagged: bool = False,
        degraded: bool = False,
        raw_output: str | None = None,
        cost_usd: float = 0.0,
    ) -> int:
        version = self.next_cumulative_version(experiment_id)
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO research_cumulative(
                    experiment_id, created_at, version, rederived_from, up_to_session_id,
                    up_to_session_count, spec_hash, drift_flagged, degraded,
                    report_json, raw_output, cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id, _utcnow_iso(), version, rederived_from, up_to_session_id,
                    up_to_session_count, spec_hash, int(bool(drift_flagged)),
                    int(bool(degraded)),
                    json.dumps(report) if report is not None else None,
                    raw_output, cost_usd,
                ),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _cumulative_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        d["report"] = _loads(d.get("report_json"))
        return d

    def latest_cumulative(self, experiment_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM research_cumulative WHERE experiment_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (experiment_id,),
            ).fetchone()
        return self._cumulative_row(row)

    def cumulative_version(self, experiment_id: str, version: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM research_cumulative WHERE experiment_id = ? AND version = ?",
                (experiment_id, version),
            ).fetchone()
        return self._cumulative_row(row)

    def cumulative_versions(self, experiment_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT version, created_at, up_to_session_count, drift_flagged, degraded, "
                "cost_usd FROM research_cumulative WHERE experiment_id = ? ORDER BY version DESC",
                (experiment_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- operator-response advisories ----------

    def has_advisory(self, ben_contact_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM research_advisories WHERE ben_contact_id = ?", (ben_contact_id,)
            ).fetchone()
        return row is not None

    def add_advisory(
        self,
        *,
        ben_contact_id: int,
        session_id: int | None,
        invocation_num: int | None,
        agent_message: str | None,
        advisory: Any = None,
        raw_output: str | None = None,
        posted: bool = False,
        cost_usd: float = 0.0,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO research_advisories(
                    ben_contact_id, session_id, invocation_num, created_at, agent_message,
                    advisory_json, raw_output, posted, cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ben_contact_id) DO UPDATE SET
                    advisory_json = excluded.advisory_json, raw_output = excluded.raw_output,
                    posted = excluded.posted, cost_usd = excluded.cost_usd
                """,
                (
                    ben_contact_id, session_id, invocation_num, _utcnow_iso(), agent_message,
                    json.dumps(advisory) if advisory is not None else None, raw_output,
                    int(bool(posted)), cost_usd,
                ),
            )
            rid = cur.lastrowid
            if not rid:
                row = conn.execute(
                    "SELECT id FROM research_advisories WHERE ben_contact_id = ?", (ben_contact_id,)
                ).fetchone()
                rid = row[0] if row else 0
        return int(rid)

    # ---------- design-proposal reviews ----------

    def add_proposal(
        self, *, experiment_id: str, session_id: int | None, proposal: Any,
        raw_output: str | None = None, status: str = "open",
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO research_proposals(
                    experiment_id, created_at, session_id, proposal_json, status, raw_output
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (experiment_id, _utcnow_iso(), session_id,
                 json.dumps(proposal) if proposal is not None else None, status, raw_output),
            )
            return int(cur.lastrowid)

    def recent_proposals(self, experiment_id: str | None = None, n: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if experiment_id:
                rows = conn.execute(
                    "SELECT * FROM research_proposals WHERE experiment_id = ? ORDER BY id DESC LIMIT ?",
                    (experiment_id, n),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM research_proposals ORDER BY id DESC LIMIT ?", (n,)
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["proposal"] = _loads(d.get("proposal_json"))
            out.append(d)
        return out

    def recent_advisories(self, n: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM research_advisories ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["advisory"] = _loads(d.get("advisory_json"))
            out.append(d)
        return out
