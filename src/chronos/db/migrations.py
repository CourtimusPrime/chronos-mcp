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


def _migrate_add_web_url(conn: sqlite3.Connection) -> None:
    """Add web_url column and backfill Gmail permalink for existing messages."""
    if "web_url" in _existing_columns(conn, "messages"):
        return
    conn.execute("ALTER TABLE messages ADD COLUMN web_url TEXT")
    conn.execute("""
        UPDATE messages SET web_url =
            'https://mail.google.com/mail/u/0/#all/' || provider_message_id
    """)
    logger.info("Migration: added web_url column.")


def _backfill_body_text(conn: sqlite3.Connection) -> None:
    """Ensure body_text is clean plain text for all messages."""
    import html as html_mod
    rows = conn.execute(
        "SELECT id, body_text, body_html FROM messages"
    ).fetchall()
    updates = []
    for row in rows:
        body_text = row["body_text"]
        body_html = row["body_html"]
        new_text = body_text

        if not body_text and body_html:
            new_text = _strip_html(body_html)
        elif body_text and _looks_like_html(body_text):
            new_text = _strip_html(body_html if body_html else body_text)

        # Unescape HTML entities regardless of origin
        if new_text:
            new_text = html_mod.unescape(new_text)

        if new_text != body_text:
            updates.append((new_text, row["id"]))

    if updates:
        conn.executemany("UPDATE messages SET body_text = ? WHERE id = ?", updates)
        logger.info("Cleaned body_text for %d messages.", len(updates))


def _migrate_fts_add_attachment_names(conn: sqlite3.Connection) -> None:
    """Rebuild messages_fts virtual table to include attachment_names column."""
    try:
        conn.execute("SELECT attachment_names FROM messages_fts LIMIT 1")
        return  # column already present
    except sqlite3.OperationalError:
        pass

    from chronos.db.schema import MESSAGES_FTS_DDL, MESSAGES_FTS_TRIGGERS
    conn.execute("DROP TRIGGER IF EXISTS messages_fts_insert")
    conn.execute("DROP TRIGGER IF EXISTS messages_fts_delete")
    conn.execute("DROP TRIGGER IF EXISTS messages_fts_update")
    conn.execute("DROP TABLE IF EXISTS messages_fts")
    conn.execute(MESSAGES_FTS_DDL)
    for trigger in MESSAGES_FTS_TRIGGERS:
        conn.execute(trigger)
    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
    logger.info("Migration: rebuilt messages_fts with attachment_names column.")


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply all DDL statements to initialize or upgrade the database schema."""
    try:
        for ddl in ALL_DDL:
            ddl = ddl.strip()
            if ddl:
                conn.execute(ddl)
        _migrate_add_from_name(conn)
        _migrate_add_web_url(conn)
        _backfill_body_text(conn)
        _migrate_fts_add_attachment_names(conn)
        conn.commit()
        logger.debug("Schema migrations applied successfully.")
    except sqlite3.Error as e:
        logger.error("Failed to apply migrations: %s", e)
        raise
