"""All error codes from PRD §12."""
from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import JSONResponse


def error_response(code: str, message: str, status_code: int) -> JSONResponse:
    """Return a standard error envelope response."""
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "data": None,
            "error": {"code": code, "message": message},
        },
    )


def ok_response(data) -> dict:
    """Return a standard success envelope."""
    return {"ok": True, "data": data, "error": None}


def list_response(items: list, total: int, limit: int, offset: int) -> dict:
    """Return a paginated list envelope."""
    return ok_response({
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + len(items)) < total,
    })


# Error code constants
ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
MESSAGE_NOT_FOUND = "MESSAGE_NOT_FOUND"
EVENT_NOT_FOUND = "EVENT_NOT_FOUND"
THREAD_NOT_FOUND = "THREAD_NOT_FOUND"
CALENDAR_NOT_FOUND = "CALENDAR_NOT_FOUND"
WRITE_NOT_ALLOWED = "WRITE_NOT_ALLOWED"
INVALID_SQL = "INVALID_SQL"
QUERY_RESULT_EMPTY = "QUERY_RESULT_EMPTY"
EVENT_READ_ONLY = "EVENT_READ_ONLY"
PENDING_CHANGE_EXISTS = "PENDING_CHANGE_EXISTS"
SYNC_NOT_RUNNING = "SYNC_NOT_RUNNING"
PROVIDER_ERROR = "PROVIDER_ERROR"
INVALID_BODY = "INVALID_BODY"
