"""Tests for the HTTP REST API."""
from __future__ import annotations

import json
import time

import pytest

from ulid import ULID


def _insert_account(conn, provider="gmail", email="test@example.com", alias="test"):
    """Helper to insert a test account."""
    acct_id = str(ULID())
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO accounts (id, provider, email, display_name, auth_type, auth_data, sync_enabled, created_at)
        VALUES (?, ?, ?, ?, 'oauth2', '{"token_file": "/tmp/test_token.json"}', 1, ?)
        """,
        (acct_id, provider, email, alias, now_ms),
    )
    conn.commit()
    return acct_id


def _insert_calendar(conn, acct_id, name="Test Calendar"):
    """Helper to insert a test calendar."""
    cal_id = str(ULID())
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO calendars (id, account_id, provider_calendar_id, name, created_at)
        VALUES (?, ?, 'cal001', ?, ?)
        """,
        (cal_id, acct_id, name, now_ms),
    )
    conn.commit()
    return cal_id


def _insert_message(conn, acct_id, subject="Test Subject", body_text="Hello world"):
    """Helper to insert a test message."""
    msg_id = str(ULID())
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO messages (
            id, account_id, provider_message_id, subject, from_address,
            to_addresses, date_unix, body_text, labels, sync_state, created_at
        ) VALUES (?, ?, ?, ?, 'sender@example.com', '["to@example.com"]',
                  ?, ?, '["INBOX", "UNREAD"]', 'synced', ?)
        """,
        (msg_id, acct_id, f"provider-{msg_id}", subject, now_ms, body_text, now_ms),
    )
    conn.commit()
    return msg_id


def _get_conn(api_client):
    """Get a DB connection from the test client's app state."""
    from chronos.db.connection import get_connection
    db_path = api_client.app.state.db_path
    return get_connection(db_path)


class TestAccountsEndpoint:
    def test_list_accounts_empty(self, api_client):
        resp = api_client.get("/v1/accounts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["items"] == []
        assert data["data"]["total"] == 0

    def test_list_accounts_with_data(self, api_client):
        conn = _get_conn(api_client)
        _insert_account(conn, email="user@gmail.com")
        conn.close()

        resp = api_client.get("/v1/accounts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert len(data["data"]["items"]) == 1
        account = data["data"]["items"][0]
        assert account["email"] == "user@gmail.com"
        assert "auth_data" not in account  # auth_data must never be returned

    def test_response_envelope_structure(self, api_client):
        """All responses must have ok, data, error fields."""
        resp = api_client.get("/v1/accounts")
        data = resp.json()
        assert "ok" in data
        assert "data" in data
        assert "error" in data
        assert data["error"] is None


class TestMessagesEndpoint:
    def test_list_messages_empty(self, api_client):
        resp = api_client.get("/v1/messages")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["items"] == []

    def test_list_messages_with_data(self, api_client):
        conn = _get_conn(api_client)
        acct_id = _insert_account(conn)
        _insert_message(conn, acct_id, subject="Hello World")
        conn.close()

        resp = api_client.get("/v1/messages")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]["items"]) == 1
        msg = data["data"]["items"][0]
        assert msg["subject"] == "Hello World"
        assert "body_html" not in msg  # body_html excluded from list

    def test_get_message_not_found(self, api_client):
        resp = api_client.get("/v1/messages/nonexistent")
        assert resp.status_code == 404
        data = resp.json()
        assert data["ok"] is False
        assert data["error"]["code"] == "MESSAGE_NOT_FOUND"

    def test_get_message_full(self, api_client):
        conn = _get_conn(api_client)
        acct_id = _insert_account(conn)
        msg_id = _insert_message(conn, acct_id, subject="Full Message Test")
        conn.close()

        resp = api_client.get(f"/v1/messages/{msg_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["subject"] == "Full Message Test"
        assert "body_html" in data["data"]  # full message includes body_html

    def test_filter_by_label(self, api_client):
        conn = _get_conn(api_client)
        acct_id = _insert_account(conn)
        _insert_message(conn, acct_id, subject="Inbox Message")
        conn.close()

        resp = api_client.get("/v1/messages?label=INBOX")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]["items"]) >= 1

    def test_fts_search(self, api_client):
        """FTS5 search via q parameter."""
        conn = _get_conn(api_client)
        acct_id = _insert_account(conn)
        _insert_message(conn, acct_id, subject="Budget Review Meeting")
        _insert_message(conn, acct_id, subject="Unrelated Subject")
        conn.close()

        resp = api_client.get("/v1/messages?q=Budget")
        assert resp.status_code == 200
        data = resp.json()
        items = data["data"]["items"]
        assert len(items) >= 1
        subjects = [m["subject"] for m in items]
        assert "Budget Review Meeting" in subjects


class TestEventsEndpoint:
    def test_list_events_empty(self, api_client):
        resp = api_client.get("/v1/events")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["items"] == []

    def test_get_event_not_found(self, api_client):
        resp = api_client.get("/v1/events/nonexistent")
        assert resp.status_code == 404
        data = resp.json()
        assert data["ok"] is False
        assert data["error"]["code"] == "EVENT_NOT_FOUND"

    def test_create_event_pending(self, api_client):
        """POST /v1/events creates pending_change and event with sync_state=pending."""
        conn = _get_conn(api_client)
        acct_id = _insert_account(conn, provider="google_calendar")
        cal_id = _insert_calendar(conn, acct_id)
        conn.close()

        now_ms = int(time.time() * 1000)
        resp = api_client.post("/v1/events", json={
            "account_id": acct_id,
            "calendar_id": cal_id,
            "title": "Test Event",
            "start_unix": now_ms,
            "end_unix": now_ms + 3600000,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        result = data["data"]
        assert "event_id" in result
        assert "pending_change_id" in result
        assert result["status"] == "pending"

        # Verify event row has sync_state=pending
        conn = _get_conn(api_client)
        event = conn.execute(
            "SELECT sync_state FROM events WHERE id = ?", (result["event_id"],)
        ).fetchone()
        assert event["sync_state"] == "pending"

        # Verify pending_changes row exists
        change = conn.execute(
            "SELECT status FROM pending_changes WHERE id = ?", (result["pending_change_id"],)
        ).fetchone()
        assert change["status"] == "pending"
        conn.close()

    def test_create_event_calendar_not_found(self, api_client):
        now_ms = int(time.time() * 1000)
        resp = api_client.post("/v1/events", json={
            "account_id": "nonexistent",
            "calendar_id": "nonexistent",
            "title": "Test",
            "start_unix": now_ms,
            "end_unix": now_ms + 3600000,
        })
        # error_response returns 404 HTTP status with ok:false envelope
        data = resp.json()
        assert data["ok"] is False
        assert data["error"]["code"] == "CALENDAR_NOT_FOUND"

    def test_delete_event_not_found(self, api_client):
        resp = api_client.delete("/v1/events/nonexistent")
        data = resp.json()
        assert data["ok"] is False
        assert data["error"]["code"] == "EVENT_NOT_FOUND"


class TestQueryEndpoint:
    def test_select_empty_db(self, api_client):
        resp = api_client.post("/v1/query", json={"sql": "SELECT * FROM accounts"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        result = data["data"]
        assert "columns" in result
        assert "rows" in result
        assert "row_count" in result
        assert "truncated" in result

    def test_write_not_allowed_insert(self, api_client):
        resp = api_client.post("/v1/query", json={
            "sql": "INSERT INTO accounts (id) VALUES ('test')"
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert data["error"]["code"] == "WRITE_NOT_ALLOWED"

    def test_write_not_allowed_update(self, api_client):
        resp = api_client.post("/v1/query", json={
            "sql": "UPDATE accounts SET email='x' WHERE 1=1"
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["error"]["code"] == "WRITE_NOT_ALLOWED"

    def test_write_not_allowed_delete(self, api_client):
        resp = api_client.post("/v1/query", json={
            "sql": "DELETE FROM accounts"
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["error"]["code"] == "WRITE_NOT_ALLOWED"

    def test_write_not_allowed_drop(self, api_client):
        resp = api_client.post("/v1/query", json={
            "sql": "DROP TABLE accounts"
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["error"]["code"] == "WRITE_NOT_ALLOWED"

    def test_cross_table_select(self, api_client):
        conn = _get_conn(api_client)
        acct_id = _insert_account(conn, email="cross@example.com")
        _insert_message(conn, acct_id, subject="Cross-table query test")
        conn.close()

        resp = api_client.post("/v1/query", json={
            "sql": "SELECT m.subject, a.email FROM messages m JOIN accounts a ON m.account_id = a.id"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        result = data["data"]
        assert "subject" in result["columns"]
        assert "email" in result["columns"]
        assert len(result["rows"]) >= 1

    def test_invalid_sql(self, api_client):
        resp = api_client.post("/v1/query", json={
            "sql": "SELECT * FROM nonexistent_table_xyz"
        })
        # Should return a 400 with INVALID_SQL
        data = resp.json()
        assert data["ok"] is False

    def test_params_binding(self, api_client):
        conn = _get_conn(api_client)
        acct_id = _insert_account(conn, email="params@example.com")
        conn.close()

        resp = api_client.post("/v1/query", json={
            "sql": "SELECT * FROM accounts WHERE email = ?",
            "params": ["params@example.com"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["row_count"] == 1


class TestSyncEndpoints:
    def test_sync_status_empty(self, api_client):
        resp = api_client.get("/v1/sync/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "accounts" in data["data"]
        assert "pending_changes" in data["data"]

    def test_sync_trigger_account_not_found(self, api_client):
        resp = api_client.post("/v1/sync/trigger", json={
            "account_id": "nonexistent",
            "type": "incremental",
        })
        # The engine won't be running in test, so we may get SYNC_NOT_RUNNING or ACCOUNT_NOT_FOUND
        data = resp.json()
        assert data["ok"] is False

    def test_sync_conflicts_empty(self, api_client):
        resp = api_client.get("/v1/sync/conflicts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True


class TestCalendarsEndpoint:
    def test_list_calendars_empty(self, api_client):
        resp = api_client.get("/v1/calendars")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["items"] == []

    def test_list_calendars_with_data(self, api_client):
        conn = _get_conn(api_client)
        acct_id = _insert_account(conn, provider="google_calendar")
        _insert_calendar(conn, acct_id, name="Work Calendar")
        conn.close()

        resp = api_client.get("/v1/calendars")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]["items"]) == 1
        assert data["data"]["items"][0]["name"] == "Work Calendar"
