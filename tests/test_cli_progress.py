"""Tests for live progress display rendering and uvicorn log suppression."""
from __future__ import annotations

import time

import pytest
from ulid import ULID

from chronos.db.connection import get_connection
from chronos.db.migrations import apply_migrations


# ---------------------------------------------------------------------------
# Insert helpers (mirrored from test_api.py)
# ---------------------------------------------------------------------------

def _insert_account(conn, provider="gmail", email="test@example.com", alias="test"):
    acct_id = str(ULID())
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO accounts (id, provider, email, display_name, auth_type, auth_data,
                              sync_enabled, created_at)
        VALUES (?, ?, ?, ?, 'oauth2', '{"token_file": "/tmp/test_token.json"}', 1, ?)
        """,
        (acct_id, provider, email, alias, now_ms),
    )
    conn.commit()
    return acct_id


def _insert_message(conn, acct_id, subject="Test"):
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


# ---------------------------------------------------------------------------
# Mock sync engine
# ---------------------------------------------------------------------------

class _MockSyncEngine:
    """Minimal mock of SyncEngine for unit tests."""

    def get_worker_state(self, account_id: str) -> str:
        return "syncing"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_live_progress_snapshot_renders_table(db_path):
    """_progress_snapshot returns a Rich Table with alias, provider, and count."""
    from chronos.cli.main import _progress_snapshot

    conn = get_connection(db_path)

    # Seed 2 accounts
    gmail_id = _insert_account(conn, provider="gmail", email="alice@gmail.com", alias="alice")
    gcal_id = _insert_account(conn, provider="google_calendar",
                               email="alice@gmail.com", alias="alice-cal")

    # Seed 1234 messages for gmail account
    for _ in range(1234):
        _insert_message(conn, gmail_id)

    # Seed 5 events for gcal account
    cal_id = _insert_calendar(conn, gcal_id)
    for _ in range(5):
        _insert_event(conn, gcal_id, cal_id)

    conn.close()

    engine = _MockSyncEngine()
    table = _progress_snapshot(engine, db_path)

    # Render the table to a string for assertions
    from io import StringIO
    from rich.console import Console as RichConsole
    buf = StringIO()
    rc = RichConsole(file=buf, width=120, no_color=True)
    rc.print(table)
    rendered = buf.getvalue()

    # Aliases should appear
    assert "alice" in rendered, f"Expected 'alice' in rendered table:\n{rendered}"
    # Provider names should appear
    assert "gmail" in rendered, f"Expected 'gmail' in rendered table:\n{rendered}"
    assert "google_calendar" in rendered, f"Expected 'google_calendar' in rendered table:\n{rendered}"
    # Formatted count for gmail messages (1,234)
    assert "1,234" in rendered, f"Expected '1,234' in rendered table:\n{rendered}"
    # Formatted count for calendar events (5)
    assert "5" in rendered, f"Expected '5' in rendered table:\n{rendered}"


def test_no_http_access_log_in_uvicorn_config(db_path):
    """Both uvicorn configs in _run_daemon set access_log=False and log_config=None."""
    import inspect
    from chronos.cli.main import _run_daemon

    src = inspect.getsource(_run_daemon)

    # access_log=False must appear at least twice
    assert src.count("access_log=False") >= 2, (
        "Expected access_log=False on both HTTP and MCP uvicorn.Config blocks, "
        f"found {src.count('access_log=False')} occurrence(s)"
    )

    # log_config=None must appear at least twice
    assert src.count("log_config=None") >= 2, (
        "Expected log_config=None on both HTTP and MCP uvicorn.Config blocks, "
        f"found {src.count('log_config=None')} occurrence(s)"
    )
