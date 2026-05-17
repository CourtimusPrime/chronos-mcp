"""GmailWorker: full + incremental sync, label normalization, thread mapping."""
from __future__ import annotations

import asyncio
import base64
import email.utils
import html as html_mod
import json
import logging
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

import httpx
from ulid import ULID

logger = logging.getLogger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Rate limit backoff: base 2s, max 64s, ±20% jitter
RATE_LIMIT_BASE = 2.0
RATE_LIMIT_MAX = 64.0

# Concurrent in-flight messages.get requests. Sourced from config.yml
# (settings.sync.gmail.concurrence). Gmail allows 250 quota units/sec per
# user; messages.get costs 5 units, so 10 ≈ 50 reqs/sec safely.
def _gmail_concurrency() -> int:
    from chronos.config import get
    return int(get("sync.gmail.concurrence", 10))


def _gmail_page_size() -> int:
    """messages.list page size. Smaller = smoother progress, more list calls."""
    from chronos.config import get
    return int(get("sync.gmail.page_size", 100))

# Gmail system labels — stored verbatim
SYSTEM_LABELS = {
    "INBOX", "UNREAD", "STARRED", "SENT", "DRAFT", "SPAM", "TRASH",
    "IMPORTANT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}


def _jitter(base: float) -> float:
    """Apply ±20% jitter to a value."""
    import random
    return base * (0.8 + 0.4 * random.random())


class TokenManager:
    """Manages token loading and refresh for an account."""

    def __init__(self, token_file: str):
        self.token_file = Path(token_file)
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        self._data = json.loads(self.token_file.read_text())

    def get_access_token(self) -> str:
        expiry = self._data.get("token_expiry_unix", 0)
        now_ms = int(time.time() * 1000)
        if expiry - now_ms < 300_000:
            self._refresh()
        return self._data["access_token"]

    def _refresh(self) -> None:
        logger.info("Refreshing access token for %s", self.token_file)
        refresh_data = {
            "client_id": self._data["client_id"],
            "client_secret": self._data["client_secret"],
            "refresh_token": self._data["refresh_token"],
            "grant_type": "refresh_token",
        }
        response = httpx.post(
            self._data.get("token_uri", GOOGLE_TOKEN_URI),
            data=refresh_data,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Token refresh failed: {response.text}")

        resp = response.json()
        self._data["access_token"] = resp["access_token"]
        expires_in = resp.get("expires_in", 3600)
        self._data["token_expiry_unix"] = int((time.time() + expires_in) * 1000)
        if "refresh_token" in resp:
            self._data["refresh_token"] = resp["refresh_token"]

        self.token_file.write_text(json.dumps(self._data, indent=2))

    @property
    def email(self) -> str:
        return self._data.get("email", "")


def _normalize_label(label_id: str, label_name: str) -> str:
    """Gmail system labels are stored verbatim; user labels use display name."""
    if label_id in SYSTEM_LABELS:
        return label_id
    return label_name


def _decode_base64url(data: str) -> str:
    """Decode base64url-encoded string to UTF-8."""
    # Add padding
    padded = data + "=" * (4 - len(data) % 4)
    decoded = base64.urlsafe_b64decode(padded)
    return decoded.decode("utf-8", errors="replace")


class _HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = frozenset({"p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"})
    _SKIP_TAGS = frozenset({"script", "style", "head"})

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(html_mod.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(html_mod.unescape(f"&#{name};"))


def _strip_html(raw: str) -> str:
    """Convert HTML to clean plain text."""
    extractor = _HTMLTextExtractor()
    extractor.feed(raw)
    text = "".join(extractor._parts)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _looks_like_html(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("<") and bool(re.search(r"<(html|head|body|div|table|span|p)\b", stripped[:500], re.I))


def _extract_body(payload: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract plain text and HTML body from message payload."""
    body_text = None
    body_html = None

    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime_type == "text/plain" and body_data:
        body_text = _decode_base64url(body_data)
    elif mime_type == "text/html" and body_data:
        body_html = _decode_base64url(body_data)
    elif mime_type.startswith("multipart/"):
        for part in payload.get("parts", []):
            pt, ph = _extract_body(part)
            if pt and not body_text:
                body_text = pt
            if ph and not body_html:
                body_html = ph

    # Derive clean plain text from HTML when no text/plain part exists,
    # or when the sender mislabeled an HTML payload as text/plain.
    if body_text and _looks_like_html(body_text):
        body_text = _strip_html(body_text)
    if not body_text and body_html:
        body_text = _strip_html(body_html)

    return body_text, body_html


def _extract_attachments(payload: dict) -> tuple[int, list[str]]:
    """Extract has_attachments flag and attachment filenames."""
    names = []

    def _walk(p: dict) -> None:
        if p.get("filename") and p.get("body", {}).get("attachmentId"):
            names.append(p["filename"])
        for part in p.get("parts", []):
            _walk(part)

    _walk(payload)
    return (1 if names else 0), names


def _parse_date_unix(date_str: str) -> int:
    """Parse RFC 2822 date string to unix milliseconds."""
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        return int(parsed.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


def _get_header(headers: list[dict], name: str) -> Optional[str]:
    """Get header value by name (case-insensitive)."""
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value")
    return None


class GmailWorker:
    """Syncs Gmail messages for one account."""

    def __init__(self, account_row: dict, db_path: str | None = None):
        self.account_id = account_row["id"]
        self.email = account_row["email"]
        auth_data = json.loads(account_row["auth_data"])
        self.token_manager = TokenManager(auth_data["token_file"])
        self.db_path = db_path
        self._sync_cursor = account_row.get("sync_cursor")
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
                    # Force token refresh
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
        """Full Gmail sync using users.messages.list + users.messages.get."""
        logger.info("Starting full Gmail sync for %s", self.email)
        conn = self._get_conn()
        now_ms = int(time.time() * 1000)
        sync_log_id = str(ULID())
        conn.execute(
            "INSERT INTO sync_log (id, account_id, sync_type, started_at, status) VALUES (?, ?, 'full', ?, 'running')",
            (sync_log_id, self.account_id, now_ms),
        )
        conn.commit()

        records_synced = 0
        last_history_id = None

        try:
            sem = asyncio.Semaphore(_gmail_concurrency())

            async def _fetch_one(client: httpx.AsyncClient, msg_id: str) -> dict:
                async with sem:
                    return await self._api_get(
                        client,
                        f"{GMAIL_API_BASE}/users/me/messages/{msg_id}",
                        {"format": "full"},
                    )

            page_size = _gmail_page_size()
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Get all message IDs
                page_token = None
                while True:
                    params = {"maxResults": page_size}
                    if page_token:
                        params["pageToken"] = page_token

                    data = await self._api_get(
                        client, f"{GMAIL_API_BASE}/users/me/messages", params
                    )
                    messages = data.get("messages", [])

                    # Stream completions so the SYNCED counter ticks up smoothly
                    # as each fetch finishes (vs. waiting for the whole page).
                    tasks = [
                        asyncio.create_task(_fetch_one(client, m["id"]))
                        for m in messages
                    ]
                    for fut in asyncio.as_completed(tasks):
                        try:
                            msg_data = await fut
                        except Exception as e:
                            logger.warning("Failed to fetch message: %s", e)
                            continue
                        await asyncio.to_thread(self._upsert_message, conn, msg_data)
                        records_synced += 1
                        if "historyId" in msg_data:
                            last_history_id = msg_data["historyId"]

                    page_token = data.get("nextPageToken")
                    if not page_token:
                        break

            # Update sync cursor with final historyId
            if last_history_id:
                conn.execute(
                    "UPDATE accounts SET sync_cursor = ?, last_synced_at = ? WHERE id = ?",
                    (last_history_id, int(time.time() * 1000), self.account_id),
                )
                self._sync_cursor = last_history_id

            conn.execute(
                "UPDATE sync_log SET status = 'completed', completed_at = ?, records_synced = ? WHERE id = ?",
                (int(time.time() * 1000), records_synced, sync_log_id),
            )
            conn.commit()
            logger.info("Full Gmail sync complete: %d messages for %s", records_synced, self.email)

        except Exception as e:
            logger.error("Full Gmail sync failed for %s: %s", self.email, e)
            conn.execute(
                "UPDATE sync_log SET status = 'failed', completed_at = ?, error_message = ? WHERE id = ?",
                (int(time.time() * 1000), str(e), sync_log_id),
            )
            conn.commit()
            raise
        finally:
            conn.close()

    async def incremental_sync(self) -> None:
        """Incremental Gmail sync using users.history.list."""
        if not self._sync_cursor:
            logger.info("No sync cursor for %s, running full sync", self.email)
            await self.full_sync()
            return

        logger.debug("Starting incremental Gmail sync for %s", self.email)
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
            sem = asyncio.Semaphore(_gmail_concurrency())

            async def _fetch_one(client: httpx.AsyncClient, msg_id: str) -> dict:
                async with sem:
                    return await self._api_get(
                        client,
                        f"{GMAIL_API_BASE}/users/me/messages/{msg_id}",
                        {"format": "full"},
                    )

            async with httpx.AsyncClient(timeout=30.0) as client:
                page_token = None
                new_history_id = self._sync_cursor

                page_size = _gmail_page_size()
                while True:
                    params = {"startHistoryId": self._sync_cursor, "maxResults": page_size}
                    if page_token:
                        params["pageToken"] = page_token

                    try:
                        data = await self._api_get(
                            client,
                            f"{GMAIL_API_BASE}/users/me/history",
                            params,
                        )
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 404:
                            # History expired, fall back to full sync
                            logger.warning("Gmail history expired for %s, falling back to full sync", self.email)
                            conn.close()
                            await self.full_sync()
                            return
                        raise

                    if "historyId" in data:
                        new_history_id = data["historyId"]

                    # Collect added + deleted IDs across all history items on this page
                    added_ids: list[str] = []
                    deleted_ids: list[str] = []
                    for history_item in data.get("history", []):
                        added_ids.extend(
                            a["message"]["id"] for a in history_item.get("messagesAdded", [])
                        )
                        deleted_ids.extend(
                            d["message"]["id"] for d in history_item.get("messagesDeleted", [])
                        )

                    # Only flip to "running" when there's real work — silent polls
                    # leave the display at ✓ done
                    if added_ids or deleted_ids:
                        self._current_sync_state = "running"

                    # Stream completions so SYNCED ticks up smoothly
                    if added_ids:
                        tasks = [
                            asyncio.create_task(_fetch_one(client, mid))
                            for mid in added_ids
                        ]
                        for fut in asyncio.as_completed(tasks):
                            try:
                                msg_data = await fut
                            except Exception as e:
                                logger.warning("Failed to fetch message: %s", e)
                                continue
                            await asyncio.to_thread(self._upsert_message, conn, msg_data)
                            records_synced += 1

                    # Deletes are local-only; do them sequentially
                    for msg_id in deleted_ids:
                        await asyncio.to_thread(self._delete_message, conn, msg_id)
                        records_synced += 1

                        # Labels added
                        for label_change in history_item.get("labelsAdded", []):
                            msg_id = label_change["message"]["id"]
                            try:
                                msg_data = await self._api_get(
                                    client,
                                    f"{GMAIL_API_BASE}/users/me/messages/{msg_id}",
                                    {"format": "full"},
                                )
                                await asyncio.to_thread(self._upsert_message, conn, msg_data)
                            except Exception as e:
                                logger.warning("Failed to update labels for message %s: %s", msg_id, e)

                        # Labels removed
                        for label_change in history_item.get("labelsRemoved", []):
                            msg_id = label_change["message"]["id"]
                            try:
                                msg_data = await self._api_get(
                                    client,
                                    f"{GMAIL_API_BASE}/users/me/messages/{msg_id}",
                                    {"format": "full"},
                                )
                                await asyncio.to_thread(self._upsert_message, conn, msg_data)
                            except Exception as e:
                                logger.warning("Failed to update labels for message %s: %s", msg_id, e)

                    page_token = data.get("nextPageToken")
                    if not page_token:
                        break

            # Update cursor
            conn.execute(
                "UPDATE accounts SET sync_cursor = ?, last_synced_at = ? WHERE id = ?",
                (new_history_id, int(time.time() * 1000), self.account_id),
            )
            self._sync_cursor = new_history_id

            conn.execute(
                "UPDATE sync_log SET status = 'completed', completed_at = ?, records_synced = ? WHERE id = ?",
                (int(time.time() * 1000), records_synced, sync_log_id),
            )
            conn.commit()
            logger.debug("Incremental Gmail sync complete: %d records for %s", records_synced, self.email)

        except Exception as e:
            logger.error("Incremental Gmail sync failed for %s: %s", self.email, e)
            conn.execute(
                "UPDATE sync_log SET status = 'failed', completed_at = ?, error_message = ? WHERE id = ?",
                (int(time.time() * 1000), str(e), sync_log_id),
            )
            conn.commit()
            raise
        finally:
            conn.close()

    def _upsert_thread(self, conn, thread_id: str, subject: Optional[str], participants: list[str]) -> str:
        """Insert or get thread row, return internal thread ID."""
        existing = conn.execute(
            "SELECT id FROM threads WHERE account_id = ? AND provider_thread_id = ?",
            (self.account_id, thread_id),
        ).fetchone()

        if existing:
            # Update participant list and count
            conn.execute(
                "UPDATE threads SET message_count = message_count + 1, last_message_at = ? WHERE id = ?",
                (int(time.time() * 1000), existing["id"]),
            )
            return existing["id"]
        else:
            new_id = str(ULID())
            now_ms = int(time.time() * 1000)
            conn.execute(
                """
                INSERT INTO threads (id, account_id, provider_thread_id, subject, participant_addresses, message_count, last_message_at, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (new_id, self.account_id, thread_id, subject, json.dumps(participants), now_ms, now_ms),
            )
            return new_id

    def _upsert_message(self, conn, msg_data: dict) -> None:
        """Insert or update a message from Gmail API response."""
        provider_message_id = msg_data["id"]
        thread_id_provider = msg_data.get("threadId", "")
        payload = msg_data.get("payload", {})
        headers = payload.get("headers", [])

        subject = _get_header(headers, "Subject")
        from_address = _get_header(headers, "From") or ""
        from_name, _ = email.utils.parseaddr(from_address)
        to_raw = _get_header(headers, "To") or ""
        cc_raw = _get_header(headers, "Cc")
        bcc_raw = _get_header(headers, "Bcc")
        date_str = _get_header(headers, "Date") or ""
        in_reply_to = _get_header(headers, "In-Reply-To")
        references_header = _get_header(headers, "References")

        date_unix = _parse_date_unix(date_str) if date_str else int(time.time() * 1000)

        # Parse address lists
        to_addresses = json.dumps([a.strip() for a in to_raw.split(",") if a.strip()])
        cc_addresses = json.dumps([a.strip() for a in cc_raw.split(",") if a.strip()]) if cc_raw else None
        bcc_addresses = json.dumps([a.strip() for a in bcc_raw.split(",") if a.strip()]) if bcc_raw else None

        # Labels
        raw_labels = msg_data.get("labelIds", [])
        labels = json.dumps(raw_labels)  # Gmail labels are already normalized IDs

        # Body
        body_text, body_html = _extract_body(payload)

        # Attachments
        has_attachments, attachment_names = _extract_attachments(payload)
        attachment_names_json = json.dumps(attachment_names) if attachment_names else None

        # Thread
        participants = []
        if from_address:
            participants.append(from_address)
        internal_thread_id = self._upsert_thread(conn, thread_id_provider, subject, participants)

        now_ms = int(time.time() * 1000)

        # Check existing
        existing = conn.execute(
            "SELECT id, sync_state FROM messages WHERE account_id = ? AND provider_message_id = ?",
            (self.account_id, provider_message_id),
        ).fetchone()

        if existing:
            # PRD §7.2: if pending, don't overwrite
            if existing["sync_state"] == "pending":
                # Check for conflict
                pending = conn.execute(
                    "SELECT id FROM pending_changes WHERE resource_id = ? AND status = 'pending'",
                    (existing["id"],),
                ).fetchone()
                if pending:
                    conn.execute(
                        "UPDATE messages SET sync_state = 'conflict' WHERE id = ?",
                        (existing["id"],),
                    )
                    conn.commit()
                return

            conn.execute(
                """
                UPDATE messages SET
                    thread_id = ?, subject = ?, from_address = ?, from_name = ?,
                    to_addresses = ?, cc_addresses = ?, bcc_addresses = ?, date_unix = ?,
                    body_text = ?, body_html = ?, labels = ?, has_attachments = ?,
                    attachment_names = ?, in_reply_to = ?, references_header = ?,
                    sync_state = 'synced', provider_raw = ?
                WHERE id = ?
                """,
                (
                    internal_thread_id, subject, from_address, from_name,
                    to_addresses, cc_addresses, bcc_addresses, date_unix,
                    body_text, body_html, labels, has_attachments, attachment_names_json,
                    in_reply_to, references_header, json.dumps(msg_data),
                    existing["id"],
                ),
            )
        else:
            msg_id = str(ULID())
            conn.execute(
                """
                INSERT INTO messages (
                    id, account_id, thread_id, provider_message_id, subject,
                    from_address, from_name, to_addresses, cc_addresses, bcc_addresses,
                    date_unix, body_text, body_html, labels, has_attachments, attachment_names,
                    in_reply_to, references_header, sync_state, created_at, provider_raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'synced', ?, ?)
                """,
                (
                    msg_id, self.account_id, internal_thread_id, provider_message_id, subject,
                    from_address, from_name, to_addresses, cc_addresses, bcc_addresses,
                    date_unix, body_text, body_html, labels, has_attachments, attachment_names_json,
                    in_reply_to, references_header, now_ms, json.dumps(msg_data),
                ),
            )

        conn.commit()

    def _delete_message(self, conn, provider_message_id: str) -> None:
        """Delete message by provider ID (PRD §7.2: delete regardless of sync_state)."""
        row = conn.execute(
            "SELECT id, sync_state FROM messages WHERE account_id = ? AND provider_message_id = ?",
            (self.account_id, provider_message_id),
        ).fetchone()

        if not row:
            return

        # If there's a pending change, reject it
        conn.execute(
            "UPDATE pending_changes SET status = 'rejected', rejection_reason = 'provider_deleted' "
            "WHERE resource_id = ? AND status IN ('pending', 'submitted')",
            (row["id"],),
        )

        conn.execute("DELETE FROM messages WHERE id = ?", (row["id"],))
        conn.commit()

    async def run(self) -> None:
        """Main sync loop for this account (PRD §7.1)."""
        self._running = True
        # Signal to the live display that the worker is up. "ready" maps to ✓ done
        # in _state_cell but distinguishes us from a freshly-constructed "idle"
        # worker that hasn't entered its run loop yet.
        self._current_sync_state = "ready"
        polling_interval = 60  # seconds

        # Check if we need a full sync first
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
                logger.error("Initial full sync failed for %s: %s", self.email, e)
                self._current_sync_state = "error"

        while self._running:
            try:
                # Don't preemptively flip to "running" — incremental_sync flips it
                # only when actual new messages are being fetched. Otherwise the
                # display stays at ✓ done during the 60s polling cycle.
                await self._process_pending_changes()
                await self.incremental_sync()
                self._current_sync_state = "ready"
            except Exception as e:
                logger.error("Sync loop error for Gmail %s: %s", self.email, e)
                self._current_sync_state = "error"

            await asyncio.sleep(polling_interval)

    async def _process_pending_changes(self) -> None:
        """Process pending changes before fetching provider data (PRD §7.4)."""
        # Messages are read-only in v1 — no pending changes to process for Gmail
        pass

    def stop(self) -> None:
        self._running = False
