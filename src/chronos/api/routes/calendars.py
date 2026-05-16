"""Calendars endpoint."""
from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query

from chronos.api.deps import get_db
from chronos.api.errors import list_response

router = APIRouter()


def _calendar_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "provider_calendar_id": row["provider_calendar_id"],
        "name": row["name"],
        "description": row["description"],
        "color": row["color"],
        "is_primary": bool(row["is_primary"]),
        "is_read_only": bool(row["is_read_only"]),
        "timezone": row["timezone"],
        "created_at": row["created_at"],
    }


@router.get("/calendars")
def list_calendars(
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    account_id: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """GET /v1/calendars — List calendars."""
    conditions = []
    params = []

    if account_id:
        conditions.append("account_id = ?")
        params.append(account_id)

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    count_row = conn.execute(
        f"SELECT COUNT(*) FROM calendars {where_clause}", params
    ).fetchone()
    total = count_row[0]

    rows = conn.execute(
        f"SELECT * FROM calendars {where_clause} ORDER BY is_primary DESC, name LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    items = [_calendar_to_dict(r) for r in rows]
    return list_response(items, total, limit, offset)
