"""Focused test for P1-4: decay also deletes vectors from the semantic store.

Offline + fast: a REAL EpisodicStore (sqlite, temp dir), a FAKE semantic store
that just records delete_by_episode_ids(ids) calls. No embedding-model load, no
network. Pins that session_engine.run_decay:
  - passes exactly the decayed (old + un-consolidated) ids to the semantic store,
  - never targets consolidated or recent episodes,
  - still works with NO semantic arg (backward-compatible default),
  - never lets a vector-store error break the SQLite deletion.

Run:
    ./venv/bin/python tests/test_semantic_decay.py
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Case 4 deliberately triggers a logged (not raised) vector-store error; silence
# the engine logger so the expected traceback doesn't clutter the test output.
logging.getLogger("orchestrator.engine").setLevel(logging.CRITICAL)

import session_engine  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402

UTC = timezone.utc

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")


class RecordingSemantic:
    """Records delete_by_episode_ids calls; returns the count it was given."""

    def __init__(self):
        self.deleted_calls: list[list[int]] = []

    def delete_by_episode_ids(self, episode_ids: list[int]) -> int:
        self.deleted_calls.append(list(episode_ids))
        return len(episode_ids)


class ExplodingSemantic:
    """Always raises — to prove decay never breaks on a vector-store hiccup."""

    def delete_by_episode_ids(self, episode_ids: list[int]) -> int:
        raise RuntimeError("vector store unavailable")


def _insert(ep: EpisodicStore, *, invocation_num, focus="f") -> int:
    return ep.log_episode(
        invocation_num=invocation_num, current_focus=focus, actions_taken=[],
        decisions_made=None, internal_state=None, journal_entry=None,
        next_invoke_minutes=None, raw_output="{}", tokens_in=0, tokens_out=0,
        cost_usd=0.0,
    )


def _set_old(db_path: str, episode_id: int, hours: float) -> None:
    ts = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE episodes SET timestamp = ? WHERE id = ?", (ts, episode_id))
    conn.commit()
    conn.close()


def main() -> int:
    # 1. Decayed ids are forwarded to the semantic store; consolidated + recent
    #    episodes are NOT passed (only the exact ids decay removed).
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "ep.db")
        ep = EpisodicStore(db)
        old_unconsol = _insert(ep, invocation_num=1, focus="old_unconsol")
        old_consol = _insert(ep, invocation_num=2, focus="old_consol")
        recent = _insert(ep, invocation_num=3, focus="recent")
        _set_old(db, old_unconsol, 100)
        _set_old(db, old_consol, 100)
        ep.mark_consolidated([old_consol])

        sem = RecordingSemantic()
        deleted = session_engine.run_decay(ep, decay_hours=0.001, semantic=sem)

        check("SQLite deletion unchanged: old+unconsol gone",
              ep.get_episode(old_unconsol) is None)
        check("consolidated episode survives", ep.get_episode(old_consol) is not None)
        check("recent episode survives", ep.get_episode(recent) is not None)
        check("brief return value unchanged (1 decayed)", len(deleted) == 1)
        check("semantic.delete_by_episode_ids called exactly once",
              len(sem.deleted_calls) == 1)
        check("fake received exactly the decayed id",
              sem.deleted_calls == [[old_unconsol]])

    # 2. No semantic arg -> no vector deletion, identical brief (the tests' call shape).
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "ep.db")
        ep = EpisodicStore(db)
        eid = _insert(ep, invocation_num=1, focus="old")
        _set_old(db, eid, 100)
        deleted = session_engine.run_decay(ep, decay_hours=0.001)
        check("run_decay with no semantic arg still decays", len(deleted) == 1)
        check("run_decay with no semantic arg deletes from SQLite",
              ep.get_episode(eid) is None)

    # 3. Nothing stale -> semantic store is never touched.
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "ep.db")
        ep = EpisodicStore(db)
        _insert(ep, invocation_num=1, focus="recent")  # not backdated
        sem = RecordingSemantic()
        deleted = session_engine.run_decay(ep, decay_hours=72, semantic=sem)
        check("no stale episodes -> empty brief", deleted == [])
        check("no stale episodes -> semantic untouched", sem.deleted_calls == [])

    # 4. A vector-store hiccup never breaks decay: SQLite delete still happens.
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "ep.db")
        ep = EpisodicStore(db)
        eid = _insert(ep, invocation_num=1, focus="old")
        _set_old(db, eid, 100)
        deleted = session_engine.run_decay(ep, decay_hours=0.001, semantic=ExplodingSemantic())
        check("exploding semantic store: decay still returns brief", len(deleted) == 1)
        check("exploding semantic store: SQLite deletion still happened",
              ep.get_episode(eid) is None)

    print()
    if _failures:
        print(f"{_failures} check(s) FAILED")
        return 1
    print("All checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
