"""POST /v1/query — raw SQL SELECT endpoint."""
from __future__ import annotations

import re
import sqlite3
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from chronos.api.deps import get_db
from chronos.api.errors import (
    INVALID_SQL, WRITE_NOT_ALLOWED,
    error_response, ok_response,
)

router = APIRouter()

MAX_ROWS = 10_000

# Keywords that indicate non-SELECT statements
DML_DDL_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|REPLACE|MERGE|GRANT|REVOKE|ATTACH|DETACH|PRAGMA)\b",
    re.IGNORECASE,
)


class QueryBody(BaseModel):
    sql: str
    params: Optional[list[Any]] = None


@router.post("/query")
def raw_query(
    body: QueryBody,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
):
    """POST /v1/query — Execute read-only SELECT against the database."""
    sql = body.sql.strip()

    # Check for non-SELECT statements
    if DML_DDL_PATTERN.search(sql):
        return error_response(
            WRITE_NOT_ALLOWED,
            "Only SELECT statements are permitted. DML and DDL are not allowed.",
            400,
        )

    # Must start with SELECT
    if not re.match(r"^\s*SELECT\b", sql, re.IGNORECASE):
        return error_response(
            WRITE_NOT_ALLOWED,
            "Only SELECT statements are permitted.",
            400,
        )

    try:
        params = body.params or []
        cursor = conn.execute(sql, params)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []

        # Fetch up to MAX_ROWS + 1 to detect truncation
        rows = cursor.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        if truncated:
            rows = rows[:MAX_ROWS]

        return ok_response({
            "columns": columns,
            "rows": [list(row) for row in rows],
            "row_count": len(rows),
            "truncated": truncated,
        })

    except sqlite3.OperationalError as e:
        return error_response(INVALID_SQL, f"SQL error: {e}", 400)
    except Exception as e:
        return error_response(INVALID_SQL, f"Query error: {e}", 400)
