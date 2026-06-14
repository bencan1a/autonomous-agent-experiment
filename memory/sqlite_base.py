"""Shared SQLite plumbing for the instance's ``episodes.db``.

``EpisodicStore`` (memory/episodic.py) and ``ResearchStore`` (research/store.py)
open the *same* database file but own separate schemas/queries. Their connection
lifecycle and their "try ALTER; ignore duplicate column" migration idiom were
byte-identical copies; this module is the single source of truth for both so a
fix to the plumbing reaches both stores.

This is connection plumbing only — it deliberately knows nothing about either
store's schema or queries. No imports from the store modules, so it is safe to
import from either side without an import cycle.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterable, Iterator


@contextmanager
def _conn(db_path: str) -> Iterator[sqlite3.Connection]:
    """A connection that yields a ``Row``-factory cursor, commits on clean
    exit, and always closes. Identical lifecycle previously duplicated in both
    stores."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def run_idempotent_alters(
    conn: sqlite3.Connection, statements: Iterable[str]
) -> None:
    """Apply ``ALTER TABLE ... ADD COLUMN`` migrations, tolerating columns that
    already exist so re-running on an existing DB is a no-op. Any other
    OperationalError is re-raised."""
    for stmt in statements:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
