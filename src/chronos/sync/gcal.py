"""CalendarWorker: full + incremental sync, recurring events, syncToken handling."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import httpx
from ulid import ULID

from .gmail import TokenManager, RATE_LIMIT_BASE, RATE_LIMIT_MAX, _jitter

logger = logging.getLogger(__name__)

GCAL_API_BASE = "https://www.googleapis.com/calendar/v3"


def _parse_datetime_to_unix(dt: Optional[str], date: Optional[str]) -> int:
    """Parse a Google Calendar datetime or date string to unix milliseconds."""
    if dt:
        try:
            from datetime import datetime, timezone
            # Handle timezone offset formats
            if dt.endswith("Z"):
                parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            else:
                parsed = datetime.fromisoformat(dt)
            return int(parsed.timestamp() * 1000)
        except Exception:
            pass
    if date:
        try:
            from datetime import datetime, timezone
            # All-day: midnight UTC
            parsed = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except Exception:
            pass
    return int(time.time() * 1000)


def _parse_event_attendees(attendees: list[dict]) -> str:
    """Parse event attendees to JSON string."""
    result = []
    for att in attendees:
        status_map = {
            "accepted": "accepted",
            "declined": "declined",
            "tentative": "tentative",
            "needsAction": "needs_action",
        }
        result.append({
            "email": att.get("email", ""),
            "name": att.get("displayName"),
            "status": status_map.get(att.get("responseStatus", "needsAction"), "needs_action"),
        })
    return json.dumps(result)


def _parse_status(status: str) -> str:
    """Map Google Calendar event status to Chronos status."""
    mapping = {"confirmed": "confirmed", "tentative": "tentative", "cancelled": "cancelled"}
    return mapping.get(status, "confirmed")


class CalendarWorker:
    """Syncs Google Calendar for one account."""

    def __init__(self, account_row: dict, db_path: str | None = None):
        self.account_id = account_row["id"]
        self.email = account_row["email"]
        auth_data = json.loads(account_row["auth_data"])
        self.token_manager = TokenManager(auth_data["token_file"])
        self.db_path = db_path
        # sync_cursor is JSON: {provider_calendar_id: syncToken}
        cursor_raw = account_row.get("sync_cursor")
        self._sync_cursors: dict = json.loads(cursor_raw) if cursor_raw else {}
        self._running = False
        self._current_sync_state = "idle"

    def get_sync_state(self) -> str:
        return self._current_sync_state

    def _get_conn(self):
        from chronos.db.connection import get_connection
        return get_connection(self.db_path)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token_manager.get_access_token()}"}

    async def _api_get(self, client: httpx.AsyncClient, url: str, params: dict = None) -> dict:
        """Make an API GET request with exponential backoff on 429."""
        backoff = RATE_LIMIT_BASE
        while True:
            try:
                resp = await client.get(url, headers=self._headers(), params=params or {})
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", backoff))
                    wait = min(_jitter(retry_after), RATE_LIMIT_MAX)
                    logger.warning("Rate limited, sleeping %.1fs", wait)
                    await asyncio.sleep(wait)
                    backoff = min(backoff * 2, RATE_LIMIT_MAX)
                    continue
                elif resp.status_code == 401:
                    self.token_manager._refresh()
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    self.token_manager._refresh()
                    continue
                raise

    async def full_sync(self) -> None:
        """Full calendar sync: calendarList.list + events.list per calendar."""
        logger.info("Starting full Calendar sync for %s", self.email)
        conn = self._get_conn()
        now_ms = int(time.time() * 1000)
        sync_log_id = str(ULID())
        conn.execute(
            "INSERT INTO sync_log (id, account_id, sync_type, started_at, status) VALUES (?, ?, 'full', ?, 'running')",
            (sync_log_id, self.account_id, now_ms),
        )
        conn.commit()

        records_synced = 0

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Get calendar list
                cal_data = await self._api_get(
                    client, f"{GCAL_API_BASE}/users/me/calendarList"
                )
                calendars = cal_data.get("items", [])

                for cal in calendars:
                    cal_id = await asyncio.to_thread(self._upsert_calendar, conn, cal)

                    # Sync events for this calendar
                    page_token = None
                    sync_token = None

                    while True:
                        params = {
                            "singleEvents": "false",
                            "maxResults": 2500,
                            "showDeleted": "true",
                        }
                        if page_token:
                            params["pageToken"] = page_token

                        try:
                            events_data = await self._api_get(
                                client,
                                f"{GCAL_API_BASE}/calendars/{cal['id']}/events",
                                params,
                            )
                        except httpx.HTTPStatusError as e:
                            logger.warning("Failed to sync calendar %s: %s", cal["id"], e)
                            break

                        for event in events_data.get("items", []):
                            await asyncio.to_thread(
                                self._upsert_event, conn, event, cal_id, cal["id"]
                            )
                            records_synced += 1

                        sync_token = events_data.get("nextSyncToken")
                        page_token = events_data.get("nextPageToken")

                        if not page_token:
                            break

                    if sync_token:
                        self._sync_cursors[cal["id"]] = sync_token

            # Save sync cursors
            conn.execute(
                "UPDATE accounts SET sync_cursor = ?, last_synced_at = ? WHERE id = ?",
                (json.dumps(self._sync_cursors), int(time.time() * 1000), self.account_id),
            )
            conn.execute(
                "UPDATE sync_log SET status = 'completed', completed_at = ?, records_synced = ? WHERE id = ?",
                (int(time.time() * 1000), records_synced, sync_log_id),
            )
            conn.commit()
            logger.info("Full Calendar sync complete: %d events for %s", records_synced, self.email)

        except Exception as e:
            logger.error("Full Calendar sync failed for %s: %s", self.email, e)
            conn.execute(
                "UPDATE sync_log SET status = 'failed', completed_at = ?, error_message = ? WHERE id = ?",
                (int(time.time() * 1000), str(e), sync_log_id),
            )
            conn.commit()
            raise
        finally:
            conn.close()

    async def incremental_sync(self) -> None:
        """Incremental sync using syncToken per calendar."""
        if not self._sync_cursors:
            logger.info("No sync cursors for %s, running full sync", self.email)
            await self.full_sync()
            return

        logger.debug("Starting incremental Calendar sync for %s", self.email)
        conn = self._get_conn()
        now_ms = int(time.time() * 1000)
        sync_log_id = str(ULID())
        conn.execute(
            "INSERT INTO sync_log (id, account_id, sync_type, started_at, status) VALUES (?, ?, 'incremental', ?, 'running')",
            (sync_log_id, self.account_id, now_ms),
        )
        conn.commit()

        records_synced = 0

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Get current calendar list (to discover new/removed calendars)
                cal_data = await self._api_get(
                    client, f"{GCAL_API_BASE}/users/me/calendarList"
                )
                calendars = cal_data.get("items", [])

                for cal in calendars:
                    cal_id = await asyncio.to_thread(self._upsert_calendar, conn, cal)
                    provider_cal_id = cal["id"]
                    sync_token = self._sync_cursors.get(provider_cal_id)

                    if not sync_token:
                        # New calendar, do a full sync for it
                        await self._sync_calendar_full(client, conn, cal, cal_id)
                        continue

                    # Incremental sync with syncToken
                    try:
                        events_data = await self._api_get(
                            client,
                            f"{GCAL_API_BASE}/calendars/{provider_cal_id}/events",
                            {"syncToken": sync_token, "showDeleted": "true"},
                        )
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 410:
                            # syncToken expired — fall back to full sync for this calendar
                            logger.warning("syncToken expired for calendar %s, running full sync", provider_cal_id)
                            del self._sync_cursors[provider_cal_id]
                            await self._sync_calendar_full(client, conn, cal, cal_id)
                            continue
                        raise

                    items = events_data.get("items", [])
                    if items:
                        # Only flip to "running" when there's real work — silent
                        # polls leave the display at ✓ done
                        self._current_sync_state = "running"
                    for event in items:
                        if event.get("status") == "cancelled":
                            await asyncio.to_thread(
                                self._delete_event, conn, event["id"]
                            )
                        else:
                            await asyncio.to_thread(
                                self._upsert_event, conn, event, cal_id, provider_cal_id
                            )
                        records_synced += 1

                    new_sync_token = events_data.get("nextSyncToken")
                    if new_sync_token:
                        self._sync_cursors[provider_cal_id] = new_sync_token

            # Save sync cursors
            conn.execute(
                "UPDATE accounts SET sync_cursor = ?, last_synced_at = ? WHERE id = ?",
                (json.dumps(self._sync_cursors), int(time.time() * 1000), self.account_id),
            )
            conn.execute(
                "UPDATE sync_log SET status = 'completed', completed_at = ?, records_synced = ? WHERE id = ?",
                (int(time.time() * 1000), records_synced, sync_log_id),
            )
            conn.commit()
            logger.debug("Incremental Calendar sync complete: %d events for %s", records_synced, self.email)

        except Exception as e:
            logger.error("Incremental Calendar sync failed for %s: %s", self.email, e)
            conn.execute(
                "UPDATE sync_log SET status = 'failed', completed_at = ?, error_message = ? WHERE id = ?",
                (int(time.time() * 1000), str(e), sync_log_id),
            )
            conn.commit()
            raise
        finally:
            conn.close()

    async def _sync_calendar_full(
        self, client: httpx.AsyncClient, conn, cal: dict, cal_id: str
    ) -> None:
        """Full sync for a single calendar."""
        provider_cal_id = cal["id"]
        page_token = None

        while True:
            params = {
                "singleEvents": "false",
                "maxResults": 2500,
                "showDeleted": "true",
            }
            if page_token:
                params["pageToken"] = page_token

            try:
                events_data = await self._api_get(
                    client,
                    f"{GCAL_API_BASE}/calendars/{provider_cal_id}/events",
                    params,
                )
            except Exception as e:
                logger.warning("Failed to sync calendar %s: %s", provider_cal_id, e)
                break

            for event in events_data.get("items", []):
                await asyncio.to_thread(
                    self._upsert_event, conn, event, cal_id, provider_cal_id
                )

            sync_token = events_data.get("nextSyncToken")
            page_token = events_data.get("nextPageToken")

            if not page_token:
                if sync_token:
                    self._sync_cursors[provider_cal_id] = sync_token
                break

        conn.commit()

    def _upsert_calendar(self, conn, cal: dict) -> str:
        """Insert or update a calendar row, return internal calendar ID."""
        provider_calendar_id = cal["id"]

        existing = conn.execute(
            "SELECT id FROM calendars WHERE account_id = ? AND provider_calendar_id = ?",
            (self.account_id, provider_calendar_id),
        ).fetchone()

        now_ms = int(time.time() * 1000)

        if existing:
            conn.execute(
                """
                UPDATE calendars SET
                    name = ?, description = ?, color = ?,
                    is_primary = ?, is_read_only = ?, timezone = ?,
                    provider_raw = ?
                WHERE id = ?
                """,
                (
                    cal.get("summary", ""),
                    cal.get("description"),
                    cal.get("colorId") or cal.get("backgroundColor"),
                    1 if cal.get("primary") else 0,
                    1 if cal.get("accessRole") in ("reader", "freeBusyReader") else 0,
                    cal.get("timeZone"),
                    json.dumps(cal),
                    existing["id"],
                ),
            )
            conn.commit()
            return existing["id"]
        else:
            cal_id = str(ULID())
            conn.execute(
                """
                INSERT INTO calendars (id, account_id, provider_calendar_id, name, description,
                    color, is_primary, is_read_only, timezone, provider_raw, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cal_id, self.account_id, provider_calendar_id,
                    cal.get("summary", ""),
                    cal.get("description"),
                    cal.get("colorId") or cal.get("backgroundColor"),
                    1 if cal.get("primary") else 0,
                    1 if cal.get("accessRole") in ("reader", "freeBusyReader") else 0,
                    cal.get("timeZone"),
                    json.dumps(cal),
                    now_ms,
                ),
            )
            conn.commit()
            return cal_id

    def _upsert_event(self, conn, event: dict, cal_id: str, provider_cal_id: str) -> None:
        """Insert or update an event row."""
        provider_event_id = event["id"]

        # Parse times
        start = event.get("start", {})
        end = event.get("end", {})
        start_unix = _parse_datetime_to_unix(start.get("dateTime"), start.get("date"))
        end_unix = _parse_datetime_to_unix(end.get("dateTime"), end.get("date"))
        is_all_day = 1 if "date" in start else 0

        # Parse attendees
        attendees_raw = event.get("attendees", [])
        attendees_json = _parse_event_attendees(attendees_raw) if attendees_raw else None

        # Recurring event handling
        is_recurring_master = 0
        rrule = None
        recurrence_master_id = None
        recurrence_instance_date = None

        recurrence = event.get("recurrence", [])
        recurring_event_id = event.get("recurringEventId")

        if recurrence and not recurring_event_id:
            # This is a master event
            is_recurring_master = 1
            # Extract RRULE from recurrence array
            for r in recurrence:
                if r.startswith("RRULE:"):
                    rrule = r[6:]  # Strip "RRULE:" prefix
                    break

        if recurring_event_id:
            # This is an instance; find the master's internal ID
            master_row = conn.execute(
                "SELECT id FROM events WHERE account_id = ? AND provider_event_id = ?",
                (self.account_id, recurring_event_id),
            ).fetchone()
            if master_row:
                recurrence_master_id = master_row["id"]

            original_start = event.get("originalStartTime", {})
            recurrence_instance_date = _parse_datetime_to_unix(
                original_start.get("dateTime"), original_start.get("date")
            )

        status = _parse_status(event.get("status", "confirmed"))
        organizer = event.get("organizer", {})
        organizer_address = organizer.get("email")
        conference_url = None
        conference_data = event.get("conferenceData", {})
        for ep in conference_data.get("entryPoints", []):
            if ep.get("entryPointType") == "video":
                conference_url = ep.get("uri")
                break

        now_ms = int(time.time() * 1000)

        existing = conn.execute(
            "SELECT id, sync_state FROM events WHERE account_id = ? AND provider_event_id = ?",
            (self.account_id, provider_event_id),
        ).fetchone()

        if existing:
            if existing["sync_state"] == "pending":
                # Check for conflict (PRD §7.2)
                pending = conn.execute(
                    "SELECT id FROM pending_changes WHERE resource_id = ? AND status = 'pending'",
                    (existing["id"],),
                ).fetchone()
                if pending:
                    conn.execute(
                        "UPDATE events SET sync_state = 'conflict' WHERE id = ?",
                        (existing["id"],),
                    )
                    conn.commit()
                    return
            elif existing["sync_state"] == "conflict":
                # PRD §7.3: overwrite with provider data, clear pending change
                conn.execute(
                    "DELETE FROM pending_changes WHERE resource_id = ? AND status NOT IN ('confirmed', 'rejected')",
                    (existing["id"],),
                )

            conn.execute(
                """
                UPDATE events SET
                    calendar_id = ?, title = ?, description = ?, location = ?,
                    start_unix = ?, end_unix = ?, is_all_day = ?, timezone = ?,
                    status = ?, organizer_address = ?, attendees = ?, rrule = ?,
                    is_recurring_master = ?, recurrence_master_id = ?,
                    recurrence_instance_date = ?, conference_url = ?,
                    sync_state = 'synced', provider_raw = ?
                WHERE id = ?
                """,
                (
                    cal_id,
                    event.get("summary", ""),
                    event.get("description"),
                    event.get("location"),
                    start_unix, end_unix, is_all_day,
                    start.get("timeZone") or end.get("timeZone"),
                    status, organizer_address, attendees_json, rrule,
                    is_recurring_master, recurrence_master_id,
                    recurrence_instance_date, conference_url,
                    json.dumps(event),
                    existing["id"],
                ),
            )
        else:
            event_id = str(ULID())
            conn.execute(
                """
                INSERT INTO events (
                    id, account_id, calendar_id, provider_event_id, title, description,
                    location, start_unix, end_unix, is_all_day, timezone, status,
                    organizer_address, attendees, rrule, is_recurring_master,
                    recurrence_master_id, recurrence_instance_date, conference_url,
                    sync_state, created_at, provider_raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'synced', ?, ?)
                """,
                (
                    event_id, self.account_id, cal_id, provider_event_id,
                    event.get("summary", ""),
                    event.get("description"),
                    event.get("location"),
                    start_unix, end_unix, is_all_day,
                    start.get("timeZone") or end.get("timeZone"),
                    status, organizer_address, attendees_json, rrule,
                    is_recurring_master, recurrence_master_id,
                    recurrence_instance_date, conference_url,
                    now_ms, json.dumps(event),
                ),
            )

        conn.commit()

    def _delete_event(self, conn, provider_event_id: str) -> None:
        """Delete event by provider ID."""
        row = conn.execute(
            "SELECT id, sync_state FROM events WHERE account_id = ? AND provider_event_id = ?",
            (self.account_id, provider_event_id),
        ).fetchone()

        if not row:
            return

        # PRD §7.3: if pending+delete => reject pending change
        conn.execute(
            "UPDATE pending_changes SET status = 'rejected', rejection_reason = 'provider_deleted' "
            "WHERE resource_id = ? AND status IN ('pending', 'submitted')",
            (row["id"],),
        )

        conn.execute("DELETE FROM events WHERE id = ?", (row["id"],))
        conn.commit()

    async def _process_pending_changes(self) -> None:
        """Process pending event changes before fetching provider data (PRD §7.4)."""
        conn = self._get_conn()

        try:
            pending = conn.execute(
                "SELECT pc.*, e.provider_event_id, e.calendar_id, "
                "cal.provider_calendar_id "
                "FROM pending_changes pc "
                "JOIN events e ON pc.resource_id = e.id "
                "JOIN calendars cal ON e.calendar_id = cal.id "
                "WHERE pc.status = 'pending' AND pc.resource_type = 'event' "
                "AND pc.account_id = ? "
                "ORDER BY pc.created_at ASC",
                (self.account_id,),
            ).fetchall()

            if not pending:
                conn.close()
                return

            async with httpx.AsyncClient(timeout=30.0) as client:
                for change in pending:
                    change_id = change["id"]
                    operation = change["operation"]
                    payload = json.loads(change["payload"])
                    provider_cal_id = change["provider_calendar_id"]
                    provider_event_id = change["provider_event_id"]

                    # Mark as submitted
                    now_ms = int(time.time() * 1000)
                    conn.execute(
                        "UPDATE pending_changes SET status = 'submitted', submitted_at = ? WHERE id = ?",
                        (now_ms, change_id),
                    )
                    conn.commit()

                    try:
                        if operation == "create":
                            result = await self._api_create_event(
                                client, provider_cal_id, payload
                            )
                            # Update event with provider-assigned ID
                            conn.execute(
                                """
                                UPDATE events SET
                                    provider_event_id = ?, sync_state = 'synced',
                                    provider_raw = ?
                                WHERE id = ?
                                """,
                                (result["id"], json.dumps(result), change["resource_id"]),
                            )
                            # Update sync cursor for this calendar
                            if "etag" in result:
                                pass  # syncToken will be updated on next incremental sync

                        elif operation == "update":
                            result = await self._api_update_event(
                                client, provider_cal_id, provider_event_id, payload
                            )
                            conn.execute(
                                "UPDATE events SET sync_state = 'synced', provider_raw = ? WHERE id = ?",
                                (json.dumps(result), change["resource_id"]),
                            )

                        elif operation == "delete":
                            await self._api_delete_event(
                                client, provider_cal_id, provider_event_id
                            )
                            conn.execute(
                                "DELETE FROM events WHERE id = ?", (change["resource_id"],)
                            )

                        conn.execute(
                            "UPDATE pending_changes SET status = 'confirmed', confirmed_at = ? WHERE id = ?",
                            (int(time.time() * 1000), change_id),
                        )
                        conn.commit()

                    except httpx.HTTPStatusError as e:
                        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
                        conn.execute(
                            """
                            UPDATE pending_changes SET
                                status = 'rejected', rejection_reason = ?
                            WHERE id = ?
                            """,
                            (error_msg, change_id),
                        )
                        conn.execute(
                            "UPDATE events SET sync_state = 'conflict' WHERE id = ?",
                            (change["resource_id"],),
                        )
                        conn.commit()

                    except Exception as e:
                        # Network error — leave as pending for retry
                        conn.execute(
                            "UPDATE pending_changes SET status = 'pending' WHERE id = ?",
                            (change_id,),
                        )
                        conn.commit()
                        logger.warning("Network error processing pending change %s: %s", change_id, e)

        finally:
            conn.close()

    async def _api_create_event(
        self, client: httpx.AsyncClient, cal_id: str, payload: dict
    ) -> dict:
        """Create an event via Google Calendar API."""
        resp = await client.post(
            f"{GCAL_API_BASE}/calendars/{cal_id}/events",
            headers=self._headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    async def _api_update_event(
        self, client: httpx.AsyncClient, cal_id: str, event_id: str, payload: dict
    ) -> dict:
        """Update an event via Google Calendar API (PATCH)."""
        resp = await client.patch(
            f"{GCAL_API_BASE}/calendars/{cal_id}/events/{event_id}",
            headers=self._headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    async def _api_delete_event(
        self, client: httpx.AsyncClient, cal_id: str, event_id: str
    ) -> None:
        """Delete an event via Google Calendar API."""
        resp = await client.delete(
            f"{GCAL_API_BASE}/calendars/{cal_id}/events/{event_id}",
            headers=self._headers(),
        )
        if resp.status_code == 410:
            # Already deleted
            return
        resp.raise_for_status()

    async def run(self) -> None:
        """Main sync loop for this calendar account."""
        self._running = True
        # Signal to the live display that the worker is up (see gmail.run() comment)
        self._current_sync_state = "ready"
        polling_interval = 60

        conn = self._get_conn()
        row = conn.execute(
            "SELECT sync_cursor FROM accounts WHERE id = ?", (self.account_id,)
        ).fetchone()
        conn.close()

        needs_full_sync = not row or not row["sync_cursor"]

        if needs_full_sync:
            try:
                self._current_sync_state = "running"
                await self.full_sync()
                self._current_sync_state = "ready"
            except Exception as e:
                logger.error("Initial full Calendar sync failed for %s: %s", self.email, e)
                self._current_sync_state = "error"

        while self._running:
            try:
                # Don't preemptively flip to "running" — incremental_sync flips
                # only when actual new events are being fetched. Otherwise the
                # display stays at ✓ done during the 60s polling cycle.
                await self._process_pending_changes()
                await self.incremental_sync()
                self._current_sync_state = "ready"
            except Exception as e:
                logger.error("Calendar sync loop error for %s: %s", self.email, e)
                self._current_sync_state = "error"

            await asyncio.sleep(polling_interval)

    def stop(self) -> None:
        self._running = False
