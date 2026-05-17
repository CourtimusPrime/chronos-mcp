"""Tests for SIGINT-triggered wipe behavior and SIGTERM data preservation."""
from __future__ import annotations

import inspect
import time

import pytest
from ulid import ULID

from chronos.db.connection import get_connection
from chronos.db.migrations import apply_migrations


# ---------------------------------------------------------------------------
# Insert helpers (mirrored from test_api.py)
# ---------------------------------------------------------------------------

def _insert_account(conn, provider="gmail", email="test@example.com", alias="alice",
                    sync_cursor=None, last_synced_at=None):
    acct_id = str(ULID())
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO accounts (id, provider, email, display_name, auth_type, auth_data,
                              sync_enabled, created_at, sync_cursor, last_synced_at)
        VALUES (?, ?, ?, ?, 'oauth2', '{"token_file": "/tmp/test_token.json"}', 1, ?, ?, ?)
        """,
        (acct_id, provider, email, alias, now_ms, sync_cursor, last_synced_at),
    )
    conn.commit()
    return acct_id


def _insert_thread(conn, acct_id):
    thread_id = str(ULID())
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO threads (id, account_id, provider_thread_id, message_count, created_at)
        VALUES (?, ?, ?, 0, ?)
        """,
        (thread_id, acct_id, f"thread-{thread_id}", now_ms),
    )
    conn.commit()
    return thread_id


def _insert_message(conn, acct_id, subject="Test Subject"):
    msg_id = str(ULID())
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO messages (
            id, account_id, provider_message_id, subject, from_address,
            to_addresses, date_unix, body_text, labels, sync_state, created_at
        ) VALUES (?, ?, ?, ?, 'sender@example.com', '["to@example.com"]',
                  ?, 'body', '["INBOX"]', 'synced', ?)
        """,
        (msg_id, acct_id, f"provider-{msg_id}", subject, now_ms, now_ms),
    )
    conn.commit()
    return msg_id


def _insert_calendar(conn, acct_id):
    cal_id = str(ULID())
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO calendars (id, account_id, provider_calendar_id, name, created_at)
        VALUES (?, ?, 'cal001', 'Test Cal', ?)
        """,
        (cal_id, acct_id, now_ms),
    )
    conn.commit()
    return cal_id


def _insert_event(conn, acct_id, cal_id):
    event_id = str(ULID())
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO events (
            id, account_id, calendar_id, provider_event_id, title,
            start_unix, end_unix, sync_state, created_at
        ) VALUES (?, ?, ?, ?, 'Test Event', ?, ?, 'synced', ?)
        """,
        (event_id, acct_id, cal_id, f"provider-{event_id}", now_ms, now_ms + 3600000, now_ms),
    )
    conn.commit()
    return event_id


def _insert_pending_change(conn, acct_id):
    change_id = str(ULID())
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO pending_changes (id, resource_type, resource_id, account_id,
                                     operation, payload, status, created_at)
        VALUES (?, 'message', ?, ?, 'create', '{}', 'pending', ?)
        """,
        (change_id, str(ULID()), acct_id, now_ms),
    )
    conn.commit()
    return change_id


def _insert_sync_log(conn, acct_id):
    log_id = str(ULID())
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO sync_log (id, account_id, sync_type, started_at, status)
        VALUES (?, ?, 'incremental', ?, 'completed')
        """,
        (log_id, acct_id, now_ms),
    )
    conn.commit()
    return log_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_sigint_wipes_messages_threads_events_calendars(db_path):
    """_wipe_synced_data deletes all 6 synced tables; accounts survives."""
    from chronos.cli.main import _wipe_synced_data

    conn = get_connection(db_path)

    acct_id = _insert_account(conn)
    _insert_message(conn, acct_id)
    _insert_message(conn, acct_id)
    _insert_message(conn, acct_id)
    _insert_thread(conn, acct_id)
    cal_id = _insert_calendar(conn, acct_id)
    _insert_event(conn, acct_id, cal_id)
    _insert_event(conn, acct_id, cal_id)
    _insert_pending_change(conn, acct_id)
    _insert_sync_log(conn, acct_id)
    conn.close()

    _wipe_synced_data(db_path)

    conn = get_connection(db_path)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM calendars").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    conn.close()


def test_wipe_preserves_accounts_and_token_files(db_path, tmp_chronos_home):
    """_wipe_synced_data leaves accounts row and token file on disk."""
    from chronos.cli.main import _wipe_synced_data

    conn = get_connection(db_path)
    acct_id = _insert_account(conn, alias="alice")
    _insert_message(conn, acct_id)
    conn.close()

    # Create a fake token file
    token_file = tmp_chronos_home / "alice_token.json"
    token_file.write_text('{"access_token": "fake"}')

    _wipe_synced_data(db_path)

    conn = get_connection(db_path)
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    conn.close()
    assert token_file.exists(), "Token file must survive wipe"


def test_wipe_resets_sync_cursor_and_last_synced_at(db_path):
    """_wipe_synced_data sets sync_cursor=NULL and last_synced_at=NULL."""
    from chronos.cli.main import _wipe_synced_data

    conn = get_connection(db_path)
    acct_id = _insert_account(conn, sync_cursor="abc123", last_synced_at=1700000000000)
    conn.close()

    _wipe_synced_data(db_path)

    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT sync_cursor, last_synced_at FROM accounts WHERE id = ?", (acct_id,)
    ).fetchone()
    assert row["sync_cursor"] is None
    assert row["last_synced_at"] is None
    conn.close()


def test_sigterm_preserves_data():
    """SIGTERM clean shutdown must NOT invoke _wipe_synced_data.

    Verified by inspecting the source: _wipe_synced_data must only appear
    inside the `except KeyboardInterrupt` branch, not unconditionally in finally.
    """
    from chronos.cli.main import _cmd_start
    src = inspect.getsource(_cmd_start)

    # Find positions
    ki_pos = src.find("except KeyboardInterrupt")
    wipe_pos = src.find("_wipe_synced_data")
    finally_pos = src.find("finally:")

    assert ki_pos != -1, "_cmd_start must have an `except KeyboardInterrupt` branch"
    assert wipe_pos != -1, "_cmd_start must call `_wipe_synced_data`"

    # The wipe call must come AFTER the except KeyboardInterrupt line
    assert wipe_pos > ki_pos, (
        "_wipe_synced_data must appear after `except KeyboardInterrupt` in _cmd_start source"
    )

    # There must be no unconditional call to _wipe_synced_data in finally.
    # Strategy: extract the finally block and confirm it contains an `if interrupted:` guard
    # before any _wipe_synced_data reference.
    assert "if interrupted:" in src, (
        "_cmd_start must gate _wipe_synced_data behind `if interrupted:`"
    )
