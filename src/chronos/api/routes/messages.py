"""Messages and threads endpoints."""
from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from chronos.api.deps import get_db
from chronos.api.errors import (
    MESSAGE_NOT_FOUND, THREAD_NOT_FOUND,
    error_response, list_response, ok_response,
)

router = APIRouter()


_SNIPPET_LEN = 300


def _message_to_dict(row: sqlite3.Row, include_full: bool = False) -> dict:
    """Serialize message row. List mode returns a body snippet; full mode returns the complete body."""
    body = row["body_text"]
    d = {
        "id": row["id"],
        "account_id": row["account_id"],
        "thread_id": row["thread_id"],
        "provider_message_id": row["provider_message_id"],
        "subject": row["subject"],
        "from_address": row["from_address"],
        "from_name": row["from_name"],
        "web_url": row["web_url"],
        "to_addresses": json.loads(row["to_addresses"]) if row["to_addresses"] else [],
        "cc_addresses": json.loads(row["cc_addresses"]) if row["cc_addresses"] else None,
        "bcc_addresses": json.loads(row["bcc_addresses"]) if row["bcc_addresses"] else None,
        "date_unix": row["date_unix"],
        "body_snippet": (body[:_SNIPPET_LEN] + "…") if body and len(body) > _SNIPPET_LEN else body,
        "labels": json.loads(row["labels"]) if row["labels"] else [],
        "has_attachments": bool(row["has_attachments"]),
        "attachment_names": json.loads(row["attachment_names"]) if row["attachment_names"] else None,
        "in_reply_to": row["in_reply_to"],
        "references_header": row["references_header"],
        "sync_state": row["sync_state"],
        "created_at": row["created_at"],
    }
    if include_full:
        d["body_text"] = body
        d["body_html"] = row["body_html"]
        d["provider_raw"] = row["provider_raw"]
    return d


@router.get("/messages")
def list_messages(
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    account_id: Optional[str] = None,
    q: Optional[str] = None,
    from_address: Optional[str] = None,
    label: Optional[str] = None,
    after: Optional[int] = None,
    before: Optional[int] = None,
    thread_id: Optional[str] = None,
    has_attachments: Optional[bool] = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """GET /v1/messages — List messages with optional filters."""
    conditions = []
    params = []

    if q:
        # FTS5 search
        conditions.append(
            "m.rowid IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)"
        )
        params.append(q)

    if account_id:
        conditions.append("m.account_id = ?")
        params.append(account_id)

    if from_address:
        # from_address stores the raw From header: "Name <addr>" or bare "addr"
        conditions.append("m.from_address LIKE ?")
        params.append(f"%{from_address}%")

    if label:
        conditions.append("m.labels LIKE ?")
        params.append(f'%"{label}"%')

    if after is not None:
        conditions.append("m.date_unix >= ?")
        params.append(after)

    if before is not None:
        conditions.append("m.date_unix <= ?")
        params.append(before)

    if thread_id:
        conditions.append("m.thread_id = ?")
        params.append(thread_id)

    if has_attachments is not None:
        conditions.append("m.has_attachments = ?")
        params.append(1 if has_attachments else 0)

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    count_row = conn.execute(
        f"SELECT COUNT(*) FROM messages m {where_clause}", params
    ).fetchone()
    total = count_row[0]

    rows = conn.execute(
        f"SELECT m.* FROM messages m {where_clause} ORDER BY m.date_unix DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    items = [_message_to_dict(r) for r in rows]
    return list_response(items, total, limit, offset)


@router.get("/messages/{message_id}")
def get_message(
    message_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
):
    """GET /v1/messages/:id — Get single message with full body."""
    row = conn.execute(
        "SELECT * FROM messages WHERE id = ?", (message_id,)
    ).fetchone()

    if not row:
        return error_response(MESSAGE_NOT_FOUND, f"No message with id={message_id}", 404)

    return ok_response(_message_to_dict(row, include_full=True))


@router.get("/threads/{thread_id}")
def get_thread(
    thread_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
):
    """GET /v1/threads/:id — Get thread with all messages."""
    thread = conn.execute(
        "SELECT * FROM threads WHERE id = ?", (thread_id,)
    ).fetchone()

    if not thread:
        return error_response(THREAD_NOT_FOUND, f"No thread with id={thread_id}", 404)

    messages = conn.execute(
        "SELECT * FROM messages WHERE thread_id = ? ORDER BY date_unix ASC",
        (thread_id,),
    ).fetchall()

    return ok_response({
        "id": thread["id"],
        "account_id": thread["account_id"],
        "provider_thread_id": thread["provider_thread_id"],
        "subject": thread["subject"],
        "participant_addresses": json.loads(thread["participant_addresses"]) if thread["participant_addresses"] else [],
        "message_count": thread["message_count"],
        "last_message_at": thread["last_message_at"],
        "created_at": thread["created_at"],
        "messages": [_message_to_dict(m, include_full=True) for m in messages],
    })
