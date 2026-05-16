"""Tests for DB schema, migrations, and FTS5."""
from __future__ import annotations

import json
import time

import pytest

from chronos.db.connection import get_connection
from chronos.db.migrations import apply_migrations


def test_migrations_idempotent(db_conn):
    """Applying migrations twice should not error."""
    apply_migrations(db_conn)  # second application
    # If we get here, no error was raised


def test_all_tables_created(db_conn):
    """Verify all expected tables exist after migration."""
    tables = {
        row[0]
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected = {
        "accounts", "threads", "messages", "calendars",
        "events", "pending_changes", "sync_log",
        "messages_fts", "events_fts",
    }
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"


def test_wal_mode_enabled(db_conn):
    """Verify WAL journal mode is active."""
    row = db_conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_foreign_keys_enabled(db_conn):
    """Verify foreign key enforcement is on."""
    row = db_conn.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1


def test_messages_fts_triggers(db_conn):
    """Verify FTS5 index is updated on message insert/update/delete."""
    now_ms = int(time.time() * 1000)

    # Insert account
    from ulid import ULID
    acct_id = str(ULID())
    db_conn.execute(
        """
        INSERT INTO accounts (id, provider, email, auth_type, auth_data, created_at)
        VALUES (?, 'gmail', 'test@example.com', 'oauth2', '{}', ?)
        """,
        (acct_id, now_ms),
    )

    # Insert message
    msg_id = str(ULID())
    db_conn.execute(
        """
        INSERT INTO messages (
            id, account_id, provider_message_id, subject, from_address,
            to_addresses, date_unix, sync_state, created_at
        ) VALUES (?, ?, 'msg001', 'Test Subject FTS', 'sender@example.com',
                  '[]', ?, 'synced', ?)
        """,
        (msg_id, acct_id, now_ms, now_ms),
    )
    db_conn.commit()

    # Search via FTS
    results = db_conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'Subject'"
    ).fetchall()
    assert len(results) > 0, "FTS5 insert trigger did not fire"

    # Delete and verify cleanup
    db_conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    db_conn.commit()

    results_after = db_conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'Subject'"
    ).fetchall()
    assert len(results_after) == 0, "FTS5 delete trigger did not fire"


def test_events_fts_triggers(db_conn):
    """Verify events FTS5 index is updated on insert."""
    now_ms = int(time.time() * 1000)
    from ulid import ULID

    # Insert account and calendar
    acct_id = str(ULID())
    cal_id = str(ULID())

    db_conn.execute(
        """
        INSERT INTO accounts (id, provider, email, auth_type, auth_data, created_at)
        VALUES (?, 'google_calendar', 'test@example.com', 'oauth2', '{}', ?)
        """,
        (acct_id, now_ms),
    )
    db_conn.execute(
        """
        INSERT INTO calendars (id, account_id, provider_calendar_id, name, created_at)
        VALUES (?, ?, 'cal001', 'My Calendar', ?)
        """,
        (cal_id, acct_id, now_ms),
    )

    event_id = str(ULID())
    db_conn.execute(
        """
        INSERT INTO events (
            id, account_id, calendar_id, provider_event_id, title,
            start_unix, end_unix, sync_state, created_at
        ) VALUES (?, ?, ?, 'evt001', 'Team Planning Meeting', ?, ?, 'synced', ?)
        """,
        (event_id, acct_id, cal_id, now_ms, now_ms + 3600000, now_ms),
    )
    db_conn.commit()

    results = db_conn.execute(
        "SELECT rowid FROM events_fts WHERE events_fts MATCH 'Planning'"
    ).fetchall()
    assert len(results) > 0, "Events FTS5 insert trigger did not fire"


def test_cascade_delete_account(db_conn):
    """Verify cascading deletes work when account is removed."""
    now_ms = int(time.time() * 1000)
    from ulid import ULID

    acct_id = str(ULID())
    db_conn.execute(
        """
        INSERT INTO accounts (id, provider, email, auth_type, auth_data, created_at)
        VALUES (?, 'gmail', 'del@example.com', 'oauth2', '{}', ?)
        """,
        (acct_id, now_ms),
    )

    msg_id = str(ULID())
    db_conn.execute(
        """
        INSERT INTO messages (
            id, account_id, provider_message_id, from_address,
            to_addresses, date_unix, sync_state, created_at
        ) VALUES (?, ?, 'msg999', 'del@example.com', '[]', ?, 'synced', ?)
        """,
        (msg_id, acct_id, now_ms, now_ms),
    )
    db_conn.commit()

    # Delete account — should cascade
    db_conn.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))
    db_conn.commit()

    msg = db_conn.execute("SELECT id FROM messages WHERE id = ?", (msg_id,)).fetchone()
    assert msg is None, "Message should have been cascade-deleted"
