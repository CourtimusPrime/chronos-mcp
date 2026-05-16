"""Events endpoints: list, get, create, update, delete."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from chronos.api.deps import get_db
from chronos.api.errors import (
    CALENDAR_NOT_FOUND, EVENT_NOT_FOUND, EVENT_READ_ONLY, INVALID_BODY,
    PENDING_CHANGE_EXISTS,
    error_response, list_response, ok_response,
)

router = APIRouter()

from ulid import ULID


def _event_to_dict(row: sqlite3.Row, include_full: bool = False) -> dict:
    d = {
        "id": row["id"],
        "account_id": row["account_id"],
        "calendar_id": row["calendar_id"],
        "provider_event_id": row["provider_event_id"],
        "title": row["title"],
        "description": row["description"],
        "location": row["location"],
        "start_unix": row["start_unix"],
        "end_unix": row["end_unix"],
        "is_all_day": bool(row["is_all_day"]),
        "timezone": row["timezone"],
        "status": row["status"],
        "organizer_address": row["organizer_address"],
        "attendees": json.loads(row["attendees"]) if row["attendees"] else None,
        "rrule": row["rrule"],
        "is_recurring_master": bool(row["is_recurring_master"]),
        "recurrence_master_id": row["recurrence_master_id"],
        "recurrence_instance_date": row["recurrence_instance_date"],
        "conference_url": row["conference_url"],
        "source_message_id": row["source_message_id"],
        "sync_state": row["sync_state"],
        "created_at": row["created_at"],
    }
    if include_full:
        d["provider_raw"] = row["provider_raw"]
    return d


@router.get("/events")
def list_events(
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    account_id: Optional[str] = None,
    calendar_id: Optional[str] = None,
    q: Optional[str] = None,
    after: Optional[int] = None,
    before: Optional[int] = None,
    status: Optional[str] = None,
    attendee: Optional[str] = None,
    is_recurring_master: Optional[bool] = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """GET /v1/events — List events with optional filters."""
    conditions = []
    params = []

    if q:
        conditions.append(
            "e.rowid IN (SELECT rowid FROM events_fts WHERE events_fts MATCH ?)"
        )
        params.append(q)

    if account_id:
        conditions.append("e.account_id = ?")
        params.append(account_id)

    if calendar_id:
        conditions.append("e.calendar_id = ?")
        params.append(calendar_id)

    if after is not None:
        conditions.append("e.start_unix >= ?")
        params.append(after)

    if before is not None:
        conditions.append("e.start_unix <= ?")
        params.append(before)

    if status:
        conditions.append("e.status = ?")
        params.append(status)

    if attendee:
        conditions.append("e.attendees LIKE ?")
        params.append(f'%"{attendee}"%')

    if is_recurring_master is not None:
        conditions.append("e.is_recurring_master = ?")
        params.append(1 if is_recurring_master else 0)

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    count_row = conn.execute(
        f"SELECT COUNT(*) FROM events e {where_clause}", params
    ).fetchone()
    total = count_row[0]

    rows = conn.execute(
        f"SELECT e.* FROM events e {where_clause} ORDER BY e.start_unix ASC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    items = [_event_to_dict(r) for r in rows]
    return list_response(items, total, limit, offset)


@router.get("/events/{event_id}")
def get_event(
    event_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
):
    """GET /v1/events/:id — Get single event."""
    row = conn.execute(
        "SELECT * FROM events WHERE id = ?", (event_id,)
    ).fetchone()

    if not row:
        return error_response(EVENT_NOT_FOUND, f"No event with id={event_id}", 404)

    return ok_response(_event_to_dict(row, include_full=True))


class CreateEventBody(BaseModel):
    account_id: str
    calendar_id: str
    title: str
    start_unix: int
    end_unix: int
    description: Optional[str] = None
    location: Optional[str] = None
    is_all_day: bool = False
    attendees: Optional[list[dict]] = None
    conference_url: Optional[str] = None
    timezone: Optional[str] = None


@router.post("/events")
def create_event(
    body: CreateEventBody,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
):
    """POST /v1/events — Create event (optimistic pending change)."""
    # Verify calendar exists and is not read-only
    cal_row = conn.execute(
        "SELECT * FROM calendars WHERE id = ?", (body.calendar_id,)
    ).fetchone()

    if not cal_row:
        return error_response(CALENDAR_NOT_FOUND, f"No calendar with id={body.calendar_id}", 404)

    if cal_row["is_read_only"]:
        return error_response(EVENT_READ_ONLY, "This calendar is read-only.", 403)

    now_ms = int(time.time() * 1000)
    event_id = str(ULID())
    change_id = str(ULID())

    # Optimistically create event row with sync_state='pending'
    attendees_json = json.dumps(body.attendees) if body.attendees else None
    conn.execute(
        """
        INSERT INTO events (
            id, account_id, calendar_id, provider_event_id, title, description,
            location, start_unix, end_unix, is_all_day, timezone, status,
            attendees, conference_url, sync_state, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, 'pending', ?)
        """,
        (
            event_id, body.account_id, body.calendar_id, f"pending-{event_id}",
            body.title, body.description, body.location,
            body.start_unix, body.end_unix, 1 if body.is_all_day else 0,
            body.timezone, attendees_json, body.conference_url, now_ms,
        ),
    )

    # Build payload for Google Calendar API
    payload = {
        "summary": body.title,
        "start": {},
        "end": {},
    }
    if body.is_all_day:
        from datetime import datetime, timezone
        start_dt = datetime.fromtimestamp(body.start_unix / 1000, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(body.end_unix / 1000, tz=timezone.utc)
        payload["start"]["date"] = start_dt.strftime("%Y-%m-%d")
        payload["end"]["date"] = end_dt.strftime("%Y-%m-%d")
    else:
        from datetime import datetime, timezone
        start_dt = datetime.fromtimestamp(body.start_unix / 1000, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(body.end_unix / 1000, tz=timezone.utc)
        payload["start"]["dateTime"] = start_dt.isoformat()
        payload["end"]["dateTime"] = end_dt.isoformat()
        if body.timezone:
            payload["start"]["timeZone"] = body.timezone
            payload["end"]["timeZone"] = body.timezone

    if body.description:
        payload["description"] = body.description
    if body.location:
        payload["location"] = body.location
    if body.attendees:
        payload["attendees"] = [{"email": a.get("email")} for a in body.attendees]
    if body.conference_url:
        payload["conferenceData"] = {
            "entryPoints": [{"entryPointType": "video", "uri": body.conference_url}]
        }

    # Create pending change
    conn.execute(
        """
        INSERT INTO pending_changes (id, resource_type, resource_id, account_id, operation, payload, status, created_at)
        VALUES (?, 'event', ?, ?, 'create', ?, 'pending', ?)
        """,
        (change_id, event_id, body.account_id, json.dumps(payload), now_ms),
    )
    conn.commit()

    return ok_response({
        "event_id": event_id,
        "pending_change_id": change_id,
        "status": "pending",
    })


class UpdateEventBody(BaseModel):
    title: Optional[str] = None
    start_unix: Optional[int] = None
    end_unix: Optional[int] = None
    description: Optional[str] = None
    location: Optional[str] = None
    attendees: Optional[list[dict]] = None
    status: Optional[str] = None


@router.patch("/events/{event_id}")
def update_event(
    event_id: str,
    body: UpdateEventBody,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
):
    """PATCH /v1/events/:id — Update event (pending change)."""
    row = conn.execute(
        "SELECT e.*, c.is_read_only FROM events e JOIN calendars c ON e.calendar_id = c.id WHERE e.id = ?",
        (event_id,),
    ).fetchone()

    if not row:
        return error_response(EVENT_NOT_FOUND, f"No event with id={event_id}", 404)

    if row["is_read_only"]:
        return error_response(EVENT_READ_ONLY, "This calendar is read-only.", 403)

    # Check for existing pending change
    existing_pending = conn.execute(
        "SELECT id FROM pending_changes WHERE resource_id = ? AND status = 'pending'",
        (event_id,),
    ).fetchone()

    if existing_pending:
        return error_response(
            PENDING_CHANGE_EXISTS,
            f"A pending change for event {event_id} already exists.",
            409,
        )

    now_ms = int(time.time() * 1000)
    change_id = str(ULID())

    # Build delta payload
    delta = {}
    update_fields = []
    update_params = []

    if body.title is not None:
        delta["summary"] = body.title
        update_fields.append("title = ?")
        update_params.append(body.title)

    if body.start_unix is not None:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(body.start_unix / 1000, tz=timezone.utc)
        delta["start"] = {"dateTime": dt.isoformat()}
        update_fields.append("start_unix = ?")
        update_params.append(body.start_unix)

    if body.end_unix is not None:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(body.end_unix / 1000, tz=timezone.utc)
        delta["end"] = {"dateTime": dt.isoformat()}
        update_fields.append("end_unix = ?")
        update_params.append(body.end_unix)

    if body.description is not None:
        delta["description"] = body.description
        update_fields.append("description = ?")
        update_params.append(body.description)

    if body.location is not None:
        delta["location"] = body.location
        update_fields.append("location = ?")
        update_params.append(body.location)

    if body.attendees is not None:
        delta["attendees"] = [{"email": a.get("email")} for a in body.attendees]
        update_fields.append("attendees = ?")
        update_params.append(json.dumps(body.attendees))

    if body.status is not None:
        delta["status"] = body.status
        update_fields.append("status = ?")
        update_params.append(body.status)

    if update_fields:
        update_fields.append("sync_state = 'pending'")
        conn.execute(
            f"UPDATE events SET {', '.join(update_fields)} WHERE id = ?",
            update_params + [event_id],
        )

    conn.execute(
        """
        INSERT INTO pending_changes (id, resource_type, resource_id, account_id, operation, payload, status, created_at)
        VALUES (?, 'event', ?, ?, 'update', ?, 'pending', ?)
        """,
        (change_id, event_id, row["account_id"], json.dumps(delta), now_ms),
    )
    conn.commit()

    return ok_response({
        "event_id": event_id,
        "pending_change_id": change_id,
        "status": "pending",
    })


@router.delete("/events/{event_id}")
def delete_event(
    event_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
):
    """DELETE /v1/events/:id — Create delete pending change."""
    row = conn.execute(
        "SELECT e.*, c.is_read_only FROM events e JOIN calendars c ON e.calendar_id = c.id WHERE e.id = ?",
        (event_id,),
    ).fetchone()

    if not row:
        return error_response(EVENT_NOT_FOUND, f"No event with id={event_id}", 404)

    if row["is_read_only"]:
        return error_response(EVENT_READ_ONLY, "This calendar is read-only.", 403)

    # Check for existing pending change
    existing_pending = conn.execute(
        "SELECT id FROM pending_changes WHERE resource_id = ? AND status = 'pending'",
        (event_id,),
    ).fetchone()

    if existing_pending:
        return error_response(
            PENDING_CHANGE_EXISTS,
            f"A pending change for event {event_id} already exists.",
            409,
        )

    now_ms = int(time.time() * 1000)
    change_id = str(ULID())

    conn.execute(
        """
        INSERT INTO pending_changes (id, resource_type, resource_id, account_id, operation, payload, status, created_at)
        VALUES (?, 'event', ?, ?, 'delete', '{}', 'pending', ?)
        """,
        (change_id, event_id, row["account_id"], now_ms),
    )
    # Don't delete local row until confirmed
    conn.execute(
        "UPDATE events SET sync_state = 'pending' WHERE id = ?", (event_id,)
    )
    conn.commit()

    return ok_response({
        "event_id": event_id,
        "pending_change_id": change_id,
        "status": "pending",
    })
