"""Sync status, trigger, and conflicts endpoints."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from chronos.api.deps import get_db, get_sync_engine
from chronos.api.errors import (
    ACCOUNT_NOT_FOUND, INVALID_BODY, SYNC_NOT_RUNNING,
    error_response, ok_response,
)

router = APIRouter()


def _pending_change_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "resource_type": row["resource_type"],
        "resource_id": row["resource_id"],
        "account_id": row["account_id"],
        "operation": row["operation"],
        "status": row["status"],
        "created_at": row["created_at"],
        "submitted_at": row["submitted_at"],
        "confirmed_at": row["confirmed_at"],
        "rejection_reason": row["rejection_reason"],
    }


@router.get("/sync/status")
def get_sync_status(
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    request: Request,
):
    """GET /v1/sync/status — Sync status for all accounts."""
    sync_engine = get_sync_engine(request)

    accounts = conn.execute("SELECT * FROM accounts").fetchall()
    account_statuses = []

    for acct in accounts:
        acct_id = acct["id"]

        # Count pending changes
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM pending_changes WHERE account_id = ? AND status = 'pending'",
            (acct_id,),
        ).fetchone()[0]

        # Count conflicts
        conflict_count_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE account_id = ? AND sync_state = 'conflict'",
            (acct_id,),
        ).fetchone()[0]

        conflict_count_messages = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE account_id = ? AND sync_state = 'conflict'",
            (acct_id,),
        ).fetchone()[0]

        # Get sync state from engine
        if sync_engine:
            sync_state = sync_engine.get_worker_state(acct_id)
        else:
            sync_state = "idle"

        account_statuses.append({
            "account_id": acct_id,
            "email": acct["email"],
            "provider": acct["provider"],
            "last_synced_at": acct["last_synced_at"],
            "sync_state": sync_state,
            "pending_changes_count": pending_count,
            "conflict_count": conflict_count_events + conflict_count_messages,
        })

    # Get all pending changes
    pending_rows = conn.execute(
        "SELECT * FROM pending_changes WHERE status IN ('pending', 'submitted') ORDER BY created_at DESC"
    ).fetchall()

    return ok_response({
        "accounts": account_statuses,
        "pending_changes": [_pending_change_to_dict(r) for r in pending_rows],
    })


class TriggerSyncBody(BaseModel):
    account_id: str
    type: Optional[str] = "incremental"


@router.post("/sync/trigger")
async def trigger_sync(
    body: TriggerSyncBody,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    request: Request,
):
    """POST /v1/sync/trigger — Trigger immediate sync for an account."""
    # Verify account exists
    acct = conn.execute(
        "SELECT id FROM accounts WHERE id = ?", (body.account_id,)
    ).fetchone()

    if not acct:
        return error_response(ACCOUNT_NOT_FOUND, f"No account with id={body.account_id}", 404)

    sync_engine = get_sync_engine(request)
    if not sync_engine:
        return error_response(
            SYNC_NOT_RUNNING, "Sync engine is not running.", 503
        )

    sync_type = body.type if body.type in ("full", "incremental") else "incremental"
    triggered = await sync_engine.trigger_sync(body.account_id, sync_type)

    if not triggered:
        return error_response(ACCOUNT_NOT_FOUND, f"No account with id={body.account_id}", 404)

    return ok_response({"started": True, "account_id": body.account_id})


@router.get("/sync/conflicts")
def get_conflicts(
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
):
    """GET /v1/sync/conflicts — All rejected pending changes and conflicting resources."""
    rejected_changes = conn.execute(
        "SELECT * FROM pending_changes WHERE status = 'rejected' ORDER BY created_at DESC"
    ).fetchall()

    conflict_events = conn.execute(
        "SELECT * FROM events WHERE sync_state = 'conflict'"
    ).fetchall()

    conflict_messages = conn.execute(
        "SELECT * FROM messages WHERE sync_state = 'conflict'"
    ).fetchall()

    return ok_response({
        "rejected_changes": [_pending_change_to_dict(r) for r in rejected_changes],
        "conflict_events": [{"id": r["id"], "account_id": r["account_id"], "title": r["title"]} for r in conflict_events],
        "conflict_messages": [{"id": r["id"], "account_id": r["account_id"], "subject": r["subject"]} for r in conflict_messages],
    })
