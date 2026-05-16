"""GET /v1/accounts endpoint."""
from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends

from chronos.api.deps import get_db
from chronos.api.errors import list_response, ok_response

router = APIRouter()


def _account_to_dict(row: sqlite3.Row) -> dict:
    """Serialize account row (never include auth_data)."""
    return {
        "id": row["id"],
        "provider": row["provider"],
        "email": row["email"],
        "display_name": row["display_name"],
        "sync_enabled": bool(row["sync_enabled"]),
        "last_synced_at": row["last_synced_at"],
        "created_at": row["created_at"],
    }


@router.get("/accounts")
def list_accounts(conn: Annotated[sqlite3.Connection, Depends(get_db)]):
    """GET /v1/accounts — Returns all accounts."""
    rows = conn.execute(
        "SELECT * FROM accounts ORDER BY created_at"
    ).fetchall()

    items = [_account_to_dict(r) for r in rows]
    return list_response(items, len(items), 50, 0)
