"""Apply schema migrations on database open."""
from __future__ import annotations

import logging
import sqlite3

from .schema import ALL_DDL
from chronos.sync.gmail import _strip_html, _looks_like_html

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


def _backfill_body_text(conn: sqlite3.Connection) -> None:
    """Derive body_text from body_html for rows that lack clean plain text."""
    rows = conn.execute(
        "SELECT id, body_text, body_html FROM messages WHERE body_html IS NOT NULL"
    ).fetchall()
    updates = []
    for row in rows:
        body_text = row["body_text"]
        body_html = row["body_html"]
        if (not body_text or _looks_like_html(body_text)) and body_html:
            updates.append((_strip_html(body_html), row["id"]))
    if updates:
        conn.executemany("UPDATE messages SET body_text = ? WHERE id = ?", updates)
        logger.info("Backfilled body_text for %d messages.", len(updates))


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply all DDL statements to initialize or upgrade the database schema."""
    try:
        for ddl in ALL_DDL:
            ddl = ddl.strip()
            if ddl:
                conn.execute(ddl)
        _migrate_add_from_name(conn)
        _backfill_body_text(conn)
        conn.commit()
        logger.debug("Schema migrations applied successfully.")
    except sqlite3.Error as e:
        logger.error("Failed to apply migrations: %s", e)
        raise
