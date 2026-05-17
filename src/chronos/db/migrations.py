"""Apply schema migrations on database open."""
from __future__ import annotations

import logging
import sqlite3

from .schema import ALL_DDL

logger = logging.getLogger(__name__)


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_add_from_name(conn: sqlite3.Connection) -> None:
    """Add from_name column and backfill from from_address."""
    if "from_name" in _existing_columns(conn, "messages"):
        return
    conn.execute("ALTER TABLE messages ADD COLUMN from_name TEXT")
    # Extract display name: "Adam Hoult <addr>" → "Adam Hoult", bare addr → ""
    conn.execute("""
        UPDATE messages SET from_name =
            CASE
                WHEN INSTR(from_address, '<') > 0
                THEN TRIM(TRIM(SUBSTR(from_address, 1, INSTR(from_address, '<') - 1)), ' "')
                ELSE ''
            END
    """)
    logger.info("Migration: added from_name column and backfilled %d rows.",
                conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply all DDL statements to initialize or upgrade the database schema."""
    try:
        for ddl in ALL_DDL:
            ddl = ddl.strip()
            if ddl:
                conn.execute(ddl)
        _migrate_add_from_name(conn)
        conn.commit()
        logger.debug("Schema migrations applied successfully.")
    except sqlite3.Error as e:
        logger.error("Failed to apply migrations: %s", e)
        raise
