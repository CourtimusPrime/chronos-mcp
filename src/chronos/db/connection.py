"""SQLite connection management with required PRAGMAs."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def get_db_path() -> str:
    """Return the SQLite database file path from environment or default."""
    db_path = os.environ.get("CHRONOS_DB_PATH")
    if db_path:
        return db_path
    chronos_home = os.environ.get("CHRONOS_HOME", str(Path.home() / ".chronos"))
    return str(Path(chronos_home) / "chronos.db")


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection and apply required PRAGMAs."""
    if db_path is None:
        db_path = get_db_path()

    # Ensure directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Apply required PRAGMAs on every connection open (PRD §5.1)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.commit()

    return conn
