"""FastMCP server with all 12 tools proxying to the HTTP API."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def _build_mcp(http_port: int = 7070) -> FastMCP:
    """Create and configure the FastMCP server."""
    mcp = FastMCP("chronos")
    base_url = f"http://127.0.0.1:{http_port}"

    async def _get(path: str, params: dict = None) -> dict:
        """Make async GET to HTTP API."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{base_url}{path}", params=params or {})
            resp.raise_for_status()
            return resp.json()

    async def _post(path: str, body: dict = None) -> dict:
        """Make async POST to HTTP API."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{base_url}{path}", json=body or {})
            resp.raise_for_status()
            return resp.json()

    async def _patch(path: str, body: dict = None) -> dict:
        """Make async PATCH to HTTP API."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.patch(f"{base_url}{path}", json=body or {})
            resp.raise_for_status()
            return resp.json()

    async def _delete(path: str) -> dict:
        """Make async DELETE to HTTP API."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(f"{base_url}{path}")
            resp.raise_for_status()
            return resp.json()

    @mcp.tool()
    async def list_accounts() -> dict:
        """Returns all authenticated accounts and their sync status.

        Returns an array of account objects with id, provider, email, display_name,
        sync_enabled, last_synced_at.
        """
        return await _get("/v1/accounts")

    @mcp.tool()
    async def search_messages(
        q: Optional[str] = None,
        account_id: Optional[str] = None,
        from_address: Optional[str] = None,
        label: Optional[str] = None,
        after: Optional[int] = None,
        before: Optional[int] = None,
        thread_id: Optional[str] = None,
        has_attachments: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Search email messages with optional full-text and structured filters.

        Args:
            q: Full-text search query. Searches subject, body, and from_address.
               Supports FTS5 MATCH syntax (e.g. "meeting budget", meeting AND budget).
            account_id: Limit to one account by its ID.
            from_address: Filter by sender email address (substring match on raw From header).
            label: Return only messages that contain this label (e.g. "INBOX", "UNREAD").
            after: Unix milliseconds lower bound on message date.
            before: Unix milliseconds upper bound on message date.
            thread_id: Return only messages in this thread.
            has_attachments: Filter by attachment presence.
            limit: Max results to return. Default 50, max 500.
            offset: Pagination offset. Default 0.

        Returns:
            Paginated list of message objects (no body_html, no provider_raw).
        """
        params = {}
        if q is not None:
            params["q"] = q
        if account_id is not None:
            params["account_id"] = account_id
        if from_address is not None:
            params["from_address"] = from_address
        if label is not None:
            params["label"] = label
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        if thread_id is not None:
            params["thread_id"] = thread_id
        if has_attachments is not None:
            params["has_attachments"] = str(has_attachments).lower()
        params["limit"] = limit
        params["offset"] = offset
        return await _get("/v1/messages", params)

    @mcp.tool()
    async def get_message(id: str) -> dict:
        """Retrieve a single message by its Chronos ID, including full HTML body and provider raw response.

        Args:
            id: Chronos message ID (ULID).

        Returns:
            Full message object including body_html and provider_raw.
        """
        return await _get(f"/v1/messages/{id}")

    @mcp.tool()
    async def get_thread(thread_id: str) -> dict:
        """Retrieve all messages in a thread, ordered oldest to newest.

        Args:
            thread_id: Chronos thread ID (ULID).

        Returns:
            Thread metadata plus array of full message objects.
        """
        return await _get(f"/v1/threads/{thread_id}")

    @mcp.tool()
    async def list_events(
        account_id: Optional[str] = None,
        calendar_id: Optional[str] = None,
        q: Optional[str] = None,
        after: Optional[int] = None,
        before: Optional[int] = None,
        status: Optional[str] = None,
        attendee: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List calendar events with optional time range and structured filters.

        Args:
            account_id: Limit to one account.
            calendar_id: Limit to one calendar.
            q: Full-text search across title, description, and location.
            after: Unix milliseconds lower bound on start_unix.
            before: Unix milliseconds upper bound on start_unix.
            status: ENUM: confirmed | tentative | cancelled.
            attendee: Return only events where this email address appears in attendees.
            limit: Default 50, max 500.
            offset: Default 0.

        Returns:
            Paginated list of event objects.
        """
        params = {}
        if account_id is not None:
            params["account_id"] = account_id
        if calendar_id is not None:
            params["calendar_id"] = calendar_id
        if q is not None:
            params["q"] = q
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        if status is not None:
            params["status"] = status
        if attendee is not None:
            params["attendee"] = attendee
        params["limit"] = limit
        params["offset"] = offset
        return await _get("/v1/events", params)

    @mcp.tool()
    async def get_event(id: str) -> dict:
        """Retrieve a single event by its Chronos ID.

        Args:
            id: Chronos event ID (ULID).

        Returns:
            Full event object including provider_raw.
        """
        return await _get(f"/v1/events/{id}")

    @mcp.tool()
    async def create_event(
        account_id: str,
        calendar_id: str,
        title: str,
        start_unix: int,
        end_unix: int,
        description: Optional[str] = None,
        location: Optional[str] = None,
        is_all_day: bool = False,
        attendees: Optional[list] = None,
        conference_url: Optional[str] = None,
    ) -> dict:
        """Create a calendar event. The event is submitted to the provider asynchronously.
        Returns immediately with a pending_change ID and an optimistic local event ID.

        Args:
            account_id: Account to create the event under.
            calendar_id: Calendar to create the event in.
            title: Event title.
            start_unix: Event start time in unix milliseconds.
            end_unix: Event end time in unix milliseconds.
            description: Event description / body.
            location: Location string.
            is_all_day: Default false. If true, start_unix and end_unix must be midnight UTC.
            attendees: Array of objects: [{"email": "...", "name": "..."}].
            conference_url: Video call URL.

        Returns:
            {"event_id": "...", "pending_change_id": "...", "status": "pending"}
        """
        body = {
            "account_id": account_id,
            "calendar_id": calendar_id,
            "title": title,
            "start_unix": start_unix,
            "end_unix": end_unix,
        }
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location
        if is_all_day:
            body["is_all_day"] = is_all_day
        if attendees is not None:
            body["attendees"] = attendees
        if conference_url is not None:
            body["conference_url"] = conference_url
        return await _post("/v1/events", body)

    @mcp.tool()
    async def update_event(
        id: str,
        title: Optional[str] = None,
        start_unix: Optional[int] = None,
        end_unix: Optional[int] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[list] = None,
        status: Optional[str] = None,
    ) -> dict:
        """Update an existing calendar event. Only fields provided are updated.

        Args:
            id: Chronos event ID to update.
            title: New title.
            start_unix: New start time.
            end_unix: New end time.
            description: New description.
            location: New location.
            attendees: Full replacement of attendees list.
            status: ENUM: confirmed | tentative | cancelled.

        Returns:
            {"event_id": "...", "pending_change_id": "...", "status": "pending"}
        """
        body = {}
        if title is not None:
            body["title"] = title
        if start_unix is not None:
            body["start_unix"] = start_unix
        if end_unix is not None:
            body["end_unix"] = end_unix
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location
        if attendees is not None:
            body["attendees"] = attendees
        if status is not None:
            body["status"] = status
        return await _patch(f"/v1/events/{id}", body)

    @mcp.tool()
    async def delete_event(id: str) -> dict:
        """Delete a calendar event. Deletion is submitted to the provider asynchronously.
        The local row is preserved until confirmed.

        Args:
            id: Chronos event ID to delete.

        Returns:
            {"event_id": "...", "pending_change_id": "...", "status": "pending"}
        """
        return await _delete(f"/v1/events/{id}")

    @mcp.tool()
    async def get_sync_status() -> dict:
        """Returns sync status for all accounts and any pending or rejected changes.

        Returns:
            Same structure as GET /v1/sync/status.
        """
        return await _get("/v1/sync/status")

    @mcp.tool()
    async def trigger_sync(account_id: str, type: str = "incremental") -> dict:
        """Trigger an immediate sync for one account.

        Args:
            account_id: Account to sync.
            type: ENUM: full | incremental. Default incremental.

        Returns:
            {"started": true, "account_id": "..."}
        """
        return await _post("/v1/sync/trigger", {"account_id": account_id, "type": type})

    @mcp.tool()
    async def raw_query(sql: str, params: Optional[list] = None) -> dict:
        """Execute a read-only SQL SELECT against the Chronos SQLite database.
        Use this tool for cross-provider joins and complex analytical queries
        that cannot be expressed through the structured tools.

        Args:
            sql: A valid SQLite SELECT statement. Only SELECT is permitted.
            params: Positional parameter values bound to ? placeholders in the SQL.

        Returns:
            {"columns": [...], "rows": [[...]], "row_count": N, "truncated": false}
        """
        body = {"sql": sql}
        if params is not None:
            body["params"] = params
        return await _post("/v1/query", body)

    return mcp


def create_mcp_server(http_port: int = 7070) -> Any:
    """Return the FastMCP ASGI SSE app for use with uvicorn (daemon mode)."""
    return _build_mcp(http_port).sse_app()


def get_mcp_instance(http_port: int = 7070) -> FastMCP:
    """Return the raw FastMCP instance (for stdio or streamable-http transport)."""
    return _build_mcp(http_port)
