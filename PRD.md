# Chronos — agent-inbox PRD v0.1 (v1 scope: Gmail + Google Calendar)

> **Audience:** Agents and AI systems implementing or consuming this specification.
> All types, enums, and behavioral rules are exhaustive unless explicitly marked extensible.
> Every field marked REQUIRED must be present. Fields marked OPTIONAL default to `null` unless a default is stated.

---

## 1. Overview

Chronos (`agent-inbox`) is an open-source (MIT) local-first sync daemon for Linux that maintains a single queryable SQLite database of a user's Gmail and Google Calendar data. Multiple accounts per provider are supported by design — a user may authenticate any number of Gmail or Google Calendar accounts (e.g. personal and work), each synced independently into the same database. Agents interact with Chronos exclusively via an MCP server running on localhost. An HTTP API is available as the underlying transport. A SKILL.md file makes Chronos droppable into any compatible agent runtime.

**Binary name:** `chronos`  
**License:** MIT  
**Platform:** Linux (x86_64, aarch64)  
**Runtime:** Python ≥ 3.11  
**Database:** SQLite 3.35+ with FTS5 and WAL mode  
**Credential store:** `~/.chronos/` (created on first run)  

---

## 2. Goals

| ID | Goal |
|----|------|
| G1 | Sync Gmail and Google Calendar into a single local SQLite database |
| G2 | Expose all data to agents via a standards-compliant MCP server (localhost) |
| G3 | Expose all data to agents via a local HTTP REST API (localhost) |
| G4 | Ship a SKILL.md that makes Chronos usable from any SKILL.md-compatible agent runtime |
| G5 | Run fully offline during agent queries; no provider API calls during read operations |
| G6 | Apply provider-wins conflict resolution: local writes are proposals until confirmed by the provider |
| G7 | Support multiple authenticated accounts per provider (e.g. personal + work Gmail); each account syncs independently into the shared database |
| G8 | Distribute as a `pipx`-installable Python package |

---

## 3. Non-Goals (v1)

| ID | Excluded |
|----|----------|
| N1 | LLM processing layer (spam labelling, event extraction) — deferred to v2+ |
| N2 | Outlook / Exchange — deferred to v2+ |
| N3 | CalDAV / iCloud Calendar — deferred to v2+ |
| N4 | macOS and Windows support — deferred to a future release |
| N5 | Push notifications / webhooks from providers — polling only in v1 |
| N6 | Attachment storage — attachment metadata is stored, binary content is not |
| N7 | Sending emails — outbound message creation is out of scope |
| N8 | Multi-user / shared daemon deployments |
| N9 | Encryption of the local SQLite database |

---

## 4. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        chronos daemon                           │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                     Sync Engine                          │   │
│  │                                                          │   │
│  │  ┌──────────────────────────┐  ┌────────────────────┐   │   │
│  │  │  Gmail Worker            │  │  Google Calendar   │   │   │
│  │  │  (one task per account)  │  │  Worker            │   │   │
│  │  │                          │  │  (one per account) │   │   │
│  │  └────────────┬─────────────┘  └──────────┬─────────┘   │   │
│  └───────────────┼──────────────────────────┬┘             │   │
│                  └──────────────┬────────────┘              │   │
│                                 ▼                           │   │
│                    ┌───────────────────────┐                    │
│                    │   SQLite Database     │                    │
│                    │   (WAL mode, FTS5)    │                    │
│                    └──────────┬────────────┘                    │
│                               │                                 │
│              ┌────────────────┴────────────────┐                │
│              │                                 │                │
│   ┌──────────▼──────────┐          ┌──────────▼──────────┐     │
│   │    HTTP REST API    │          │     MCP Server       │     │
│   │  localhost:7070     │◄─────────│  localhost:7071      │     │
│   └─────────────────────┘          └─────────────────────┘     │
└─────────────────────────────────────────────────────────────────┘
```

### 4.1 Component Responsibilities

| Component | Responsibility |
|-----------|----------------|
| **Sync Engine** | Orchestrates provider workers; manages sync cursors and polling intervals |
| **Provider Workers** | One `asyncio` task per authenticated account. Multiple accounts of the same provider run as independent tasks. Handles auth refresh, API calls, and delta processing. |
| **SQLite Database** | Single file at `$CHRONOS_DB_PATH` (default: `~/.chronos/chronos.db`) |
| **HTTP REST API** | FastAPI; listens on `127.0.0.1:7070`; read + write via pending_changes |
| **MCP Server** | FastMCP wrapper over HTTP API; listens on `127.0.0.1:7071`; primary agent interface |

### 4.2 Process Model

The daemon runs as a single process. Provider workers run as `asyncio` tasks. The HTTP API and MCP server are served concurrently via `asyncio`. There is no subprocess spawning.

On startup, the daemon:
1. Opens (or creates) the SQLite database
2. Applies all pending migrations
3. Starts provider workers for all authenticated accounts
4. Starts the HTTP API server
5. Starts the MCP server
6. Begins sync loop polling

---

## 5. Data Model

### 5.1 SQLite Configuration

The following PRAGMAs are applied on every database connection open:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

### 5.2 ID Format

All primary keys use ULID (Universally Unique Lexicographically Sortable Identifier) encoded as 26-character uppercase ASCII strings. The `python-ulid` package is used for generation.

### 5.3 Timestamp Format

All timestamp fields named `*_at` or `*_unix` store milliseconds since Unix epoch as `INTEGER`. No field stores a formatted date string except `provider_raw`.

### 5.4 JSON Fields

All fields annotated `-- JSON` store a UTF-8 encoded JSON string. The schema for each is defined below. An absent optional JSON field stores `NULL`, never the string `"null"`.

---

### 5.5 Full DDL

```sql
-- ─── accounts ─────────────────────────────────────────────────────────────────
-- One row per authenticated provider account.
CREATE TABLE IF NOT EXISTS accounts (
  id            TEXT    NOT NULL PRIMARY KEY,
  provider      TEXT    NOT NULL, -- ENUM: 'gmail' | 'google_calendar'
  email         TEXT    NOT NULL, -- canonical Google account email address
  display_name  TEXT,             -- human-readable label, e.g. "Work Gmail" (OPTIONAL)
  auth_type     TEXT    NOT NULL, -- ENUM: 'oauth2'  (all v1 providers use OAuth2)
  -- auth_data stores JSON: {"token_file": "/home/user/.chronos/<alias>_token.json"}
  -- The token file is self-contained: it holds access_token, refresh_token, client_id,
  -- client_secret, token_expiry_unix, and scopes. Chronos reads and rewrites it on every
  -- token refresh. The original credentials JSON is not referenced at runtime.
  auth_data     TEXT    NOT NULL,
  sync_enabled  INTEGER NOT NULL DEFAULT 1,  -- 0 = paused, 1 = active
  last_synced_at INTEGER,                    -- unix ms of last successful sync completion
  sync_cursor   TEXT,                        -- provider-specific opaque cursor string
  created_at    INTEGER NOT NULL,
  UNIQUE(provider, email)
);

-- ─── threads ──────────────────────────────────────────────────────────────────
-- Email conversation threads. One row per provider thread.
CREATE TABLE IF NOT EXISTS threads (
  id                   TEXT    NOT NULL PRIMARY KEY,
  account_id           TEXT    NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  provider_thread_id   TEXT    NOT NULL, -- Gmail threadId
  subject              TEXT,
  -- JSON: ["address@example.com", ...]  All unique participant addresses in thread
  participant_addresses TEXT,
  message_count        INTEGER NOT NULL DEFAULT 0,
  last_message_at      INTEGER,          -- unix ms of most recent message
  provider_raw         TEXT,             -- JSON: full provider thread response
  created_at           INTEGER NOT NULL,
  UNIQUE(account_id, provider_thread_id)
);

-- ─── messages ─────────────────────────────────────────────────────────────────
-- Individual email messages.
CREATE TABLE IF NOT EXISTS messages (
  id                  TEXT    NOT NULL PRIMARY KEY,
  account_id          TEXT    NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  thread_id           TEXT    REFERENCES threads(id) ON DELETE SET NULL,
  provider_message_id TEXT    NOT NULL,  -- Gmail messageId
  subject             TEXT,
  from_address        TEXT    NOT NULL,  -- single RFC 5322 address string
  -- JSON: ["Name <addr@example.com>", ...]
  to_addresses        TEXT    NOT NULL,
  -- JSON: ["addr@example.com", ...]   OPTIONAL
  cc_addresses        TEXT,
  -- JSON: ["addr@example.com", ...]   OPTIONAL
  bcc_addresses       TEXT,
  date_unix           INTEGER NOT NULL,  -- unix ms from Date header
  body_text           TEXT,              -- plain text body; may be null if only HTML
  body_html           TEXT,              -- HTML body; may be null if only plain text
  -- JSON: ["INBOX", "UNREAD", "STARRED", ...]
  -- Normalized label names (see §6.1 for Gmail normalization rules)
  labels              TEXT,
  has_attachments     INTEGER NOT NULL DEFAULT 0,  -- 1 if message has attachments
  -- JSON: ["filename.pdf", "image.png"]   OPTIONAL
  attachment_names    TEXT,
  in_reply_to         TEXT,             -- value of In-Reply-To header; OPTIONAL
  references_header   TEXT,             -- value of References header; OPTIONAL
  -- ENUM: 'synced' | 'pending' | 'conflict'
  sync_state          TEXT    NOT NULL DEFAULT 'synced',
  created_at          INTEGER NOT NULL,
  provider_raw        TEXT,             -- JSON: full provider message response
  UNIQUE(account_id, provider_message_id)
);

-- ─── calendars ────────────────────────────────────────────────────────────────
-- One row per calendar within an account.
CREATE TABLE IF NOT EXISTS calendars (
  id                   TEXT    NOT NULL PRIMARY KEY,
  account_id           TEXT    NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  provider_calendar_id TEXT    NOT NULL,
  name                 TEXT    NOT NULL,
  description          TEXT,
  color                TEXT,            -- hex color string, e.g. "#4285F4"; OPTIONAL
  is_primary           INTEGER NOT NULL DEFAULT 0,
  is_read_only         INTEGER NOT NULL DEFAULT 0,
  timezone             TEXT,            -- IANA timezone string, e.g. "America/New_York"
  provider_raw         TEXT,            -- JSON: full provider calendar response
  created_at           INTEGER NOT NULL,
  UNIQUE(account_id, provider_calendar_id)
);

-- ─── events ───────────────────────────────────────────────────────────────────
-- Calendar events. Recurring events: one master row (is_recurring_master=1) plus
-- one row per expanded instance (recurrence_master_id is set on instances).
CREATE TABLE IF NOT EXISTS events (
  id                      TEXT    NOT NULL PRIMARY KEY,
  account_id              TEXT    NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  calendar_id             TEXT    NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
  provider_event_id       TEXT    NOT NULL,
  title                   TEXT    NOT NULL,
  description             TEXT,
  location                TEXT,
  start_unix              INTEGER NOT NULL,  -- unix ms; for all-day: midnight UTC
  end_unix                INTEGER NOT NULL,  -- unix ms; for all-day: midnight UTC next day
  is_all_day              INTEGER NOT NULL DEFAULT 0,
  timezone                TEXT,              -- IANA timezone string; OPTIONAL
  -- ENUM: 'confirmed' | 'tentative' | 'cancelled'
  status                  TEXT    NOT NULL DEFAULT 'confirmed',
  organizer_address       TEXT,              -- RFC 5322 address; OPTIONAL
  -- JSON: [{"email": str, "name": str|null, "status": "accepted"|"declined"|"tentative"|"needs_action"}, ...]
  attendees               TEXT,
  rrule                   TEXT,              -- raw RRULE string (RFC 5545); OPTIONAL; only on master
  is_recurring_master     INTEGER NOT NULL DEFAULT 0,
  recurrence_master_id    TEXT    REFERENCES events(id) ON DELETE CASCADE,  -- OPTIONAL; set on instances
  recurrence_instance_date INTEGER,          -- unix ms; identifies which instance; OPTIONAL
  conference_url          TEXT,              -- video call link; OPTIONAL
  source_message_id       TEXT    REFERENCES messages(id) ON DELETE SET NULL,  -- OPTIONAL; if event was created from email
  -- ENUM: 'synced' | 'pending' | 'conflict'
  sync_state              TEXT    NOT NULL DEFAULT 'synced',
  created_at              INTEGER NOT NULL,
  provider_raw            TEXT,              -- JSON: full provider event response
  UNIQUE(account_id, provider_event_id)
);

-- ─── pending_changes ──────────────────────────────────────────────────────────
-- Local writes that have not yet been confirmed by the provider.
-- The sync engine processes this table and submits changes to providers.
CREATE TABLE IF NOT EXISTS pending_changes (
  id               TEXT    NOT NULL PRIMARY KEY,
  resource_type    TEXT    NOT NULL,  -- ENUM: 'event'  (messages are read-only in v0)
  resource_id      TEXT    NOT NULL,  -- FK to events.id
  account_id       TEXT    NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  -- ENUM: 'create' | 'update' | 'delete'
  operation        TEXT    NOT NULL,
  -- JSON: delta object. For 'create'/'update': partial event fields to apply.
  --       For 'delete': {}
  payload          TEXT    NOT NULL,
  -- ENUM: 'pending' | 'submitted' | 'confirmed' | 'rejected'
  status           TEXT    NOT NULL DEFAULT 'pending',
  created_at       INTEGER NOT NULL,
  submitted_at     INTEGER,           -- unix ms; set when first submitted to provider
  confirmed_at     INTEGER,           -- unix ms; set when provider confirms
  rejection_reason TEXT               -- provider error message; set when status='rejected'
);

-- ─── sync_log ─────────────────────────────────────────────────────────────────
-- Append-only record of sync runs. Retained for 30 days.
CREATE TABLE IF NOT EXISTS sync_log (
  id              TEXT    NOT NULL PRIMARY KEY,
  account_id      TEXT    NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  -- ENUM: 'full' | 'incremental'
  sync_type       TEXT    NOT NULL,
  started_at      INTEGER NOT NULL,
  completed_at    INTEGER,
  records_synced  INTEGER NOT NULL DEFAULT 0,
  -- ENUM: 'running' | 'completed' | 'failed'
  status          TEXT    NOT NULL DEFAULT 'running',
  error_message   TEXT
);
```

### 5.6 Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_messages_account_date
  ON messages(account_id, date_unix DESC);

CREATE INDEX IF NOT EXISTS idx_messages_thread
  ON messages(thread_id);

CREATE INDEX IF NOT EXISTS idx_messages_from
  ON messages(from_address);

CREATE INDEX IF NOT EXISTS idx_events_account_start
  ON events(account_id, start_unix);

CREATE INDEX IF NOT EXISTS idx_events_calendar
  ON events(calendar_id);

CREATE INDEX IF NOT EXISTS idx_events_recurring_master
  ON events(recurrence_master_id);

CREATE INDEX IF NOT EXISTS idx_pending_status
  ON pending_changes(status, created_at);

CREATE INDEX IF NOT EXISTS idx_sync_log_account
  ON sync_log(account_id, started_at DESC);
```

### 5.7 Full-Text Search

```sql
-- FTS5 index over messages.
-- Triggers maintain the index automatically.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  subject,
  body_text,
  from_address,
  labels,
  content='messages',
  content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert
  AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, subject, body_text, from_address, labels)
    VALUES (new.rowid, new.subject, new.body_text, new.from_address, new.labels);
  END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete
  AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, body_text, from_address, labels)
    VALUES ('delete', old.rowid, old.subject, old.body_text, old.from_address, old.labels);
  END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update
  AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, body_text, from_address, labels)
    VALUES ('delete', old.rowid, old.subject, old.body_text, old.from_address, old.labels);
    INSERT INTO messages_fts(rowid, subject, body_text, from_address, labels)
    VALUES (new.rowid, new.subject, new.body_text, new.from_address, new.labels);
  END;

-- FTS5 index over events.
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  title,
  description,
  location,
  content='events',
  content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS events_fts_insert
  AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, title, description, location)
    VALUES (new.rowid, new.title, new.description, new.location);
  END;

CREATE TRIGGER IF NOT EXISTS events_fts_delete
  AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, title, description, location)
    VALUES ('delete', old.rowid, old.title, old.description, old.location);
  END;

CREATE TRIGGER IF NOT EXISTS events_fts_update
  AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, title, description, location)
    VALUES ('delete', old.rowid, old.title, old.description, old.location);
    INSERT INTO events_fts(rowid, title, description, location)
    VALUES (new.rowid, new.title, new.description, new.location);
  END;
```

---

## 6. Provider Specifications

### 6.1 Gmail

**Auth type:** `oauth2`  
**Scopes required:**
```
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.modify
```

**OAuth2 flow:** Authorization Code with PKCE. Redirect URI: `http://localhost:9004/callback`. The CLI opens the browser and listens on port 9004 for the callback.

**Sync model:**
- **Full sync:** Uses `users.messages.list` with `maxResults=500`, paginated via `pageToken`. Each message is fetched with `users.messages.get?format=full`. Stores `historyId` from the final page as `sync_cursor`.
- **Incremental sync:** Uses `users.history.list?startHistoryId={sync_cursor}`. Processes `messagesAdded`, `messagesDeleted`, `labelsAdded`, `labelsRemoved` history records.

**Label normalization:** Gmail system labels are stored verbatim (`INBOX`, `UNREAD`, `STARRED`, `SENT`, `DRAFT`, `SPAM`, `TRASH`). User labels are stored as their display name string.

**Thread mapping:** The `threadId` field on each Gmail message maps directly to `threads.provider_thread_id`.

**Rate limits:** Respect `Retry-After` headers. On 429, back off exponentially with base 2s, max 64s, jitter ±20%.

**Polling interval:** 60 seconds (incremental). Full sync: on first auth only, then never unless explicitly triggered by CLI.

---

### 6.2 Google Calendar

**Auth type:** `oauth2`  
**Scope required:**
```
https://www.googleapis.com/auth/calendar
```

**Token sharing:** If the user authenticates a Gmail account and a Google Calendar account with the same email, they share one OAuth2 token set. The `accounts` table holds two rows (provider='gmail' and provider='google_calendar') that reference the same credentials. Auth is performed once via `agent-inbox auth google --scopes gmail,calendar`.

**Sync model:**
- **Full sync:** `calendarList.list` → for each calendar, `events.list?singleEvents=false&maxResults=2500`. Store `nextSyncToken` per calendar in `accounts.sync_cursor` as JSON keyed by `provider_calendar_id`.
- **Incremental sync:** `events.list?syncToken={token}` per calendar. On 410 (sync token expired), fall back to full sync for that calendar.

**Recurring event handling:**
- Master events (`recurrence` array present, no `recurringEventId`): stored with `is_recurring_master=1`, `rrule` populated.
- Instance events (`recurringEventId` present): stored with `recurrence_master_id` set to the master's `id`, `recurrence_instance_date` set to `originalStartTime.dateTime` or `originalStartTime.date` in unix ms.
- v0 does NOT expand rrules locally. Only instances returned by the provider API are stored.

**Polling interval:** 60 seconds (incremental).

---

### 6.3 Outlook / Exchange — deferred to v2+

Not implemented in v1. The schema is designed to accommodate Outlook in v2+: `threads.provider_thread_id` will store Outlook `conversationId`, and the `accounts.provider` ENUM will be extended with `'outlook'`. No v1 code should assume the provider ENUM is closed.

---

### 6.4 CalDAV / iCloud Calendar — deferred to v2+

Not implemented in v1. The schema's `auth_type` field reserves space for `'basic'` auth to support iCloud app-specific passwords in v2+. No v1 code should hard-code `auth_type = 'oauth2'` as a schema constraint.

---

## 7. Sync Engine

### 7.1 Sync Loop

The sync engine runs one `asyncio.Task` per account. Each task:

```
while account.sync_enabled:
    try:
        run_incremental_sync(account)
        update accounts SET last_synced_at = now() WHERE id = account.id
    except RateLimitError as e:
        sleep(e.retry_after)
    except TokenExpiredError:
        refresh_token(account)
    except Exception as e:
        log_sync_failure(account, e)
    sleep(account.polling_interval)
```

### 7.2 Sync Record Processing

For each record returned by a provider:

1. Compute `provider_message_id` / `provider_event_id`
2. Check if a row with `(account_id, provider_*_id)` exists
3. If new: INSERT with `sync_state='synced'`
4. If existing and `sync_state='synced'`: UPDATE all fields from provider data
5. If existing and `sync_state='pending'`: **do not overwrite local row**; check `pending_changes` for in-flight changes — if the provider version conflicts with the pending change, set `sync_state='conflict'`
6. For deletions signalled by the provider: DELETE the local row regardless of `sync_state`

### 7.3 Conflict Resolution

| Local sync_state | Provider action | Resolution |
|-----------------|-----------------|------------|
| `synced` | Update | Apply provider update |
| `synced` | Delete | Delete local row |
| `pending` | Update | Mark `sync_state='conflict'`; preserve local row; expose via `GET /v1/sync/conflicts` |
| `pending` | Delete | Mark pending_change as `rejected`, set `rejection_reason='provider_deleted'`; delete local row |
| `conflict` | Update | Overwrite local row with provider data; clear pending_change |

**Provider always wins on read.** A `conflict` state is informational — the agent is notified, but the next successful sync will overwrite the local row with provider data unless the agent resolves the conflict first by deleting the pending change.

### 7.4 Pending Changes Processing

On each sync loop iteration, before fetching provider data:

1. SELECT all `pending_changes WHERE status='pending' ORDER BY created_at ASC`
2. For each pending change:
   - Submit to provider API
   - On success: set `status='confirmed'`, `confirmed_at=now()`; update local resource row with provider response; set `sync_state='synced'`
   - On provider error: set `status='rejected'`, `rejection_reason=<error>`; set `sync_state='conflict'` on the resource row
   - On network error: leave `status='pending'`; retry next cycle

---

## 8. HTTP API

**Base URL:** `http://127.0.0.1:7070`  
**Version prefix:** `/v1`  
**Authentication:** None (localhost only; no auth header required)  
**Content-Type:** `application/json` for all requests and responses  

### 8.1 Response Envelope

All responses use this envelope:

```json
{
  "ok": true,
  "data": { ... },
  "error": null
}
```

On error:
```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "ACCOUNT_NOT_FOUND",
    "message": "No account with id=01HZ..."
  }
}
```

### 8.2 Pagination

All list endpoints return:

```json
{
  "ok": true,
  "data": {
    "items": [...],
    "total": 1042,
    "limit": 50,
    "offset": 0,
    "has_more": true
  }
}
```

Default `limit`: 50. Maximum `limit`: 500.

---

### 8.3 Endpoints

#### Accounts

```
GET /v1/accounts
```
Returns all accounts. No query parameters.

Response `items` schema:
```json
{
  "id": "01HZ...",
  "provider": "gmail",
  "email": "user@example.com",
  "display_name": "Work Gmail",
  "sync_enabled": true,
  "last_synced_at": 1718000000000,
  "created_at": 1717000000000
}
```
Note: `auth_data` is never returned by any API endpoint.

---

#### Messages

```
GET /v1/messages
```

Query parameters:

| Parameter | Type | Description |
|-----------|------|-------------|
| `account_id` | TEXT | OPTIONAL. Filter by account |
| `q` | TEXT | OPTIONAL. Full-text search query (FTS5 MATCH syntax) |
| `from_address` | TEXT | OPTIONAL. Filter by sender (exact match) |
| `label` | TEXT | OPTIONAL. Filter by label (JSON array contains) |
| `after` | INTEGER | OPTIONAL. Unix ms lower bound on `date_unix` |
| `before` | INTEGER | OPTIONAL. Unix ms upper bound on `date_unix` |
| `thread_id` | TEXT | OPTIONAL. Filter by thread |
| `has_attachments` | BOOLEAN | OPTIONAL. `true` or `false` |
| `limit` | INTEGER | OPTIONAL. Default 50, max 500 |
| `offset` | INTEGER | OPTIONAL. Default 0 |

Response `items` schema: same as `messages` table minus `body_html` and `provider_raw`. To retrieve full body, use `GET /v1/messages/:id`.

```
GET /v1/messages/:id
```
Returns single message including `body_html` and `provider_raw`.

```
GET /v1/threads/:id
```
Returns thread metadata plus all messages in the thread ordered by `date_unix ASC`.

---

#### Events

```
GET /v1/events
```

Query parameters:

| Parameter | Type | Description |
|-----------|------|-------------|
| `account_id` | TEXT | OPTIONAL. Filter by account |
| `calendar_id` | TEXT | OPTIONAL. Filter by calendar |
| `q` | TEXT | OPTIONAL. Full-text search (FTS5 MATCH on title, description, location) |
| `after` | INTEGER | OPTIONAL. Unix ms lower bound on `start_unix` |
| `before` | INTEGER | OPTIONAL. Unix ms upper bound on `start_unix` |
| `status` | TEXT | OPTIONAL. ENUM: `confirmed` \| `tentative` \| `cancelled` |
| `attendee` | TEXT | OPTIONAL. Filter events where attendees JSON contains this address |
| `is_recurring_master` | BOOLEAN | OPTIONAL. Filter master vs instance rows |
| `limit` | INTEGER | OPTIONAL. Default 50, max 500 |
| `offset` | INTEGER | OPTIONAL. Default 0 |

```
GET /v1/events/:id
```
Returns single event including `provider_raw`.

```
POST /v1/events
```
Creates a pending change with `operation='create'`. Body must include all REQUIRED event fields. Returns a `pending_changes` row immediately; the event row is created optimistically in the `events` table with `sync_state='pending'`.

Required body fields: `account_id`, `calendar_id`, `title`, `start_unix`, `end_unix`.

```
PATCH /v1/events/:id
```
Creates a pending change with `operation='update'`. Body contains only the fields to update. Returns the updated `pending_changes` row.

```
DELETE /v1/events/:id
```
Creates a pending change with `operation='delete'`. Returns the created `pending_changes` row. The local event row is NOT deleted until the provider confirms.

---

#### Calendars

```
GET /v1/calendars
```

Query parameters: `account_id` (OPTIONAL).

---

#### Sync

```
GET /v1/sync/status
```

Response:
```json
{
  "ok": true,
  "data": {
    "accounts": [
      {
        "account_id": "01HZ...",
        "email": "user@example.com",
        "provider": "gmail",
        "last_synced_at": 1718000000000,
        "sync_state": "idle",
        "pending_changes_count": 2,
        "conflict_count": 0
      }
    ],
    "pending_changes": [...]
  }
}
```

`sync_state` ENUM: `idle` | `running` | `error`

```
POST /v1/sync/trigger
```
Body: `{"account_id": "01HZ...", "type": "full" | "incremental"}`. Triggers an immediate out-of-cycle sync for the specified account. `account_id` is REQUIRED.

```
GET /v1/sync/conflicts
```
Returns all rows from `pending_changes WHERE status='rejected'` and all `events/messages WHERE sync_state='conflict'`.

---

#### Raw SQL Query

```
POST /v1/query
```

Body:
```json
{
  "sql": "SELECT m.subject, e.title FROM messages m JOIN events e ON ...",
  "params": []
}
```

Rules:
- Only `SELECT` statements are accepted. The server parses the statement and returns HTTP 400 with `code: WRITE_NOT_ALLOWED` if any DML or DDL keyword is detected.
- `params` is an OPTIONAL array of positional parameters bound to `?` placeholders.
- Maximum 10,000 rows returned per query. If the result set exceeds this, the server returns the first 10,000 rows and sets `"truncated": true` in the response.

Response:
```json
{
  "ok": true,
  "data": {
    "columns": ["subject", "title"],
    "rows": [["Re: Budget", "Q3 Review"], ...],
    "row_count": 14,
    "truncated": false
  }
}
```

---

## 9. MCP Server

**Transport:** stdio (local process) or SSE (`http://127.0.0.1:7071/sse`)  
**Protocol:** MCP 1.0  
**Implementation:** FastMCP wrapping the HTTP API  
**Server name:** `chronos`  

### 9.1 Tool Definitions

Each tool description is written for LLM consumption. Parameter types follow JSON Schema.

---

#### `list_accounts`

Returns all authenticated accounts and their sync status.

**Parameters:** none

**Returns:** Array of account objects with `id`, `provider`, `email`, `display_name`, `sync_enabled`, `last_synced_at`.

---

#### `search_messages`

Search email messages with optional full-text and structured filters.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `q` | string | NO | Full-text search query. Searches subject, body, and from_address. Supports FTS5 MATCH syntax (e.g. `"meeting budget"`, `meeting AND budget`). |
| `account_id` | string | NO | Limit to one account by its ID. |
| `from_address` | string | NO | Exact sender address match. |
| `label` | string | NO | Return only messages that contain this label (e.g. `"INBOX"`, `"UNREAD"`). |
| `after` | integer | NO | Unix milliseconds lower bound on message date. |
| `before` | integer | NO | Unix milliseconds upper bound on message date. |
| `thread_id` | string | NO | Return only messages in this thread. |
| `has_attachments` | boolean | NO | Filter by attachment presence. |
| `limit` | integer | NO | Max results to return. Default 50, max 500. |
| `offset` | integer | NO | Pagination offset. Default 0. |

**Returns:** Paginated list of message objects (no `body_html`, no `provider_raw`).

---

#### `get_message`

Retrieve a single message by its Chronos ID, including full HTML body and provider raw response.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | YES | Chronos message ID (ULID). |

**Returns:** Full message object including `body_html` and `provider_raw`.

---

#### `get_thread`

Retrieve all messages in a thread, ordered oldest to newest.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `thread_id` | string | YES | Chronos thread ID (ULID). |

**Returns:** Thread metadata plus array of full message objects.

---

#### `list_events`

List calendar events with optional time range and structured filters.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `account_id` | string | NO | Limit to one account. |
| `calendar_id` | string | NO | Limit to one calendar. |
| `q` | string | NO | Full-text search across title, description, and location. |
| `after` | integer | NO | Unix milliseconds lower bound on `start_unix`. |
| `before` | integer | NO | Unix milliseconds upper bound on `start_unix`. |
| `status` | string | NO | ENUM: `confirmed` \| `tentative` \| `cancelled`. |
| `attendee` | string | NO | Return only events where this email address appears in attendees. |
| `limit` | integer | NO | Default 50, max 500. |
| `offset` | integer | NO | Default 0. |

**Returns:** Paginated list of event objects.

---

#### `get_event`

Retrieve a single event by its Chronos ID.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | YES | Chronos event ID (ULID). |

**Returns:** Full event object including `provider_raw`.

---

#### `create_event`

Create a calendar event. The event is submitted to the provider asynchronously. Returns immediately with a `pending_change` ID and an optimistic local event ID.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `account_id` | string | YES | Account to create the event under. |
| `calendar_id` | string | YES | Calendar to create the event in. |
| `title` | string | YES | Event title. |
| `start_unix` | integer | YES | Event start time in unix milliseconds. |
| `end_unix` | integer | YES | Event end time in unix milliseconds. |
| `description` | string | NO | Event description / body. |
| `location` | string | NO | Location string. |
| `is_all_day` | boolean | NO | Default false. If true, `start_unix` and `end_unix` must be midnight UTC. |
| `attendees` | array | NO | Array of objects: `[{"email": "...", "name": "..."}]`. |
| `conference_url` | string | NO | Video call URL. |

**Returns:**
```json
{
  "event_id": "01HZ...",
  "pending_change_id": "01HZ...",
  "status": "pending"
}
```

---

#### `update_event`

Update an existing calendar event. Only fields provided are updated.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | YES | Chronos event ID to update. |
| `title` | string | NO | New title. |
| `start_unix` | integer | NO | New start time. |
| `end_unix` | integer | NO | New end time. |
| `description` | string | NO | New description. |
| `location` | string | NO | New location. |
| `attendees` | array | NO | Full replacement of attendees list. |
| `status` | string | NO | ENUM: `confirmed` \| `tentative` \| `cancelled`. |

**Returns:** Same as `create_event`.

---

#### `delete_event`

Delete a calendar event. Deletion is submitted to the provider asynchronously. The local row is preserved until confirmed.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `id` | string | YES | Chronos event ID to delete. |

**Returns:**
```json
{
  "event_id": "01HZ...",
  "pending_change_id": "01HZ...",
  "status": "pending"
}
```

---

#### `get_sync_status`

Returns sync status for all accounts and any pending or rejected changes.

**Parameters:** none

**Returns:** Same structure as `GET /v1/sync/status`.

---

#### `trigger_sync`

Trigger an immediate sync for one account.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `account_id` | string | YES | Account to sync. |
| `type` | string | NO | ENUM: `full` \| `incremental`. Default `incremental`. |

**Returns:** `{"started": true, "account_id": "01HZ..."}`

---

#### `raw_query`

Execute a read-only SQL SELECT against the Chronos SQLite database. Use this tool for cross-provider joins and complex analytical queries that cannot be expressed through the structured tools.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `sql` | string | YES | A valid SQLite SELECT statement. Only SELECT is permitted. |
| `params` | array | NO | Positional parameter values bound to `?` placeholders in the SQL. |

**Returns:**
```json
{
  "columns": ["col1", "col2"],
  "rows": [[...], [...]],
  "row_count": 42,
  "truncated": false
}
```

---

## 10. CLI

Binary: `chronos`

The `chronos` binary serves as both the daemon and the CLI. All subcommands that do not start the daemon are one-shot and exit cleanly. There is no separate `agent-inbox` binary.

### 10.1 Account Registration — `chronos --add`

This is the primary setup command. The user downloads a Google OAuth2 credentials JSON file from Google Cloud Console and passes it to `chronos --add`.

**Prerequisites (user performs these steps manually):**

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project (or select an existing one)
3. Enable the **Gmail API** and **Google Calendar API** for the project
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
5. Select application type: **Desktop app**
6. Download the resulting JSON file (named something like `client_secret_....json`)
7. Place it anywhere accessible, e.g. `~/my_creds.json`

**One credentials file, multiple accounts.** A single credentials JSON encodes a `client_id` + `client_secret` for a Google Cloud project. It can be reused across multiple `chronos --add` invocations to register different Google accounts. Each invocation opens a separate browser consent flow and yields an independent `refresh_token` scoped to a different Google identity. The credentials file is only read during `--add` and is not kept or referenced afterwards.

**Command:**

```
chronos --add <alias> <path-to-credentials-json>
```

| Argument | Description |
|----------|-------------|
| `alias` | Short name for this account, e.g. `personal`, `work`. Used in all subsequent CLI commands. Must match `[a-z0-9_-]+`. |
| `path-to-credentials-json` | Path to the downloaded Google OAuth2 credentials JSON file. May be reused across multiple aliases. |

**Example — two accounts from one credentials file:**

```bash
chronos --add personal ~/my_creds.json   # opens browser → signs in as user@gmail.com
chronos --add work    ~/my_creds.json   # opens browser → signs in as work@company.com
```

Each alias produces its own independent token file and its own pair of `accounts` rows.

**What `chronos --add` does:**

1. Reads the credentials JSON. Expected format:
   ```json
   {
     "installed": {
       "client_id": "...",
       "client_secret": "...",
       "auth_uri": "https://accounts.google.com/o/oauth2/auth",
       "token_uri": "https://oauth2.googleapis.com/token",
       "redirect_uris": ["http://localhost"]
     }
   }
   ```
   If the top-level key is `web` instead of `installed`, exit with error: `credentials file must use application type "Desktop app"`.

2. Opens the system browser to Google's OAuth2 consent screen with the following parameters:
   ```
   scope=https://www.googleapis.com/auth/gmail.readonly
         https://www.googleapis.com/auth/gmail.modify
         https://www.googleapis.com/auth/calendar
   access_type=offline
   prompt=consent
   ```
   `access_type=offline` ensures Google issues a `refresh_token`. `prompt=consent` forces the consent screen every time, which is required to obtain a `refresh_token` when the same `client_id` is used for a second account — without it, Google silently omits the `refresh_token` for repeat authorizations.

3. Starts a local HTTP server on `localhost:9004` to receive the OAuth2 callback.

4. Exchanges the authorization code for `access_token` and `refresh_token`.

5. Writes a **self-contained token file** to `~/.chronos/<alias>_token.json`. This file embeds the `client_id` and `client_secret` from the credentials JSON so Chronos never needs the original credentials file again:
   ```json
   {
     "email": "user@gmail.com",
     "access_token": "ya29...",
     "refresh_token": "1//0g...",
     "token_expiry_unix": 1718003600000,
     "client_id": "1234567890-abc.apps.googleusercontent.com",
     "client_secret": "GOCSPX-...",
     "token_uri": "https://oauth2.googleapis.com/token",
     "scopes": [
       "https://www.googleapis.com/auth/gmail.readonly",
       "https://www.googleapis.com/auth/gmail.modify",
       "https://www.googleapis.com/auth/calendar"
     ]
   }
   ```
   The token file is the sole credential artifact Chronos reads at runtime. The original credentials JSON is not copied, stored, or referenced after this step.

6. **Tests the endpoints** using the freshly issued tokens:
   - `GET https://gmail.googleapis.com/gmail/v1/users/me/profile` → prints email address and `messagesTotal`
   - `GET https://www.googleapis.com/calendar/v3/users/me/calendarList` → prints count of calendars found

7. On success, writes two rows to the `accounts` table (one `provider='gmail'`, one `provider='google_calendar'`) with `auth_data` set to `{"token_file": "~/.chronos/<alias>_token.json"}` and `display_name` set to the alias.

8. Prints a confirmation summary:

```
✓ Account registered: personal
  Gmail:    user@gmail.com  (12,483 messages)
  Calendar: 3 calendars found
  Tokens:   ~/.chronos/personal_token.json

Run `chronos --start` to begin syncing.
```

**Token refresh at runtime.** Whenever an `access_token` is expired or a 401 is received, the sync worker reads the token file, calls `POST https://oauth2.googleapis.com/token` with the embedded `client_id`, `client_secret`, and `refresh_token`, and writes the updated `access_token` and `token_expiry_unix` back to the token file. No user interaction is required.

**Error cases:**

| Condition | Exit message |
|-----------|-------------|
| File not found | `Error: file not found: <path>` |
| Invalid JSON | `Error: could not parse credentials file` |
| Wrong app type (`web` key) | `Error: credentials file must use application type "Desktop app"` |
| Alias already exists | `Error: alias "personal" already registered. Use --remove personal first, or choose a different alias.` |
| OAuth consent declined | `Error: Google OAuth consent was declined` |
| No refresh_token in response | `Error: Google did not return a refresh token. Ensure prompt=consent is set and the OAuth flow completed fully.` |
| Gmail API test fails | `Error: Gmail API test failed — check that Gmail API is enabled in your Google Cloud project` |
| Calendar API test fails | `Error: Calendar API test failed — check that Google Calendar API is enabled in your Google Cloud project` |

---

### 10.2 Account Management

```
chronos --list
```
Lists all registered accounts. Output: plain text table.
```
ALIAS      PROVIDER          EMAIL                LAST SYNCED
personal   gmail             user@gmail.com       2 minutes ago
personal   google_calendar   user@gmail.com       2 minutes ago
work       gmail             work@company.com     5 minutes ago
work       google_calendar   work@company.com     5 minutes ago
```

```
chronos --remove <alias>
```
Removes both the `gmail` and `google_calendar` rows for this alias from the `accounts` table. Cascades to delete all associated messages, threads, events, calendars, pending_changes, and sync_log rows. Deletes `~/.chronos/<alias>_token.json`. Does **not** revoke the OAuth token at Google.

```
chronos --test <alias>
```
Re-runs the endpoint tests from step 7 of `--add` against stored tokens. Useful for diagnosing sync failures. Prints the same confirmation summary as `--add`.

---

### 10.3 Daemon Commands

```
chronos --start [OPTIONS]
  --http-port INTEGER  HTTP API port (default: 7070)
  --mcp-port INTEGER   MCP server port (default: 7071)
  --db-path TEXT       Override CHRONOS_DB_PATH

  Starts the daemon. Blocks until SIGINT or SIGTERM.
  All accounts with sync_enabled=1 begin syncing immediately.

chronos --status
  Prints sync status for all accounts. If the daemon is not running, prints:
  "Daemon is not running. Start it with: chronos --start"

chronos --sync [<alias>] [--type full|incremental]
  Triggers an immediate sync. If alias is omitted, syncs all accounts.
  If daemon is running, delegates to POST /v1/sync/trigger.
  If daemon is not running, runs a one-shot sync and exits.

chronos --stop
  Sends SIGTERM to the running daemon process.
```

---

### 10.4 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CHRONOS_HOME` | `~/.chronos` | Directory for credentials, tokens, and database |
| `CHRONOS_DB_PATH` | `$CHRONOS_HOME/chronos.db` | SQLite database file path; overrides CHRONOS_HOME for db only |
| `CHRONOS_HTTP_PORT` | `7070` | HTTP API listen port |
| `CHRONOS_MCP_PORT` | `7071` | MCP server listen port |
| `CHRONOS_LOG_LEVEL` | `INFO` | Log level: `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |

---

## 11. SKILL.md Specification

The SKILL.md file is placed in the root of the repository. It is the agent-facing entry point for any SKILL.md-compatible agent runtime.

Required fields:

```markdown
---
name: chronos-agent-inbox
version: 0.1.0
description: >
  Local-first email and calendar inbox for AI agents. Syncs Gmail and Google Calendar
  into a local SQLite database. Query all accounts through a single MCP server on localhost.
  Supports multiple accounts per provider.
entrypoint: chronos --start
install: pipx install chronos-agent-inbox
interface:
  type: mcp
  transport: sse
  url: http://127.0.0.1:7071/sse
tools:
  - list_accounts
  - search_messages
  - get_message
  - get_thread
  - list_events
  - get_event
  - create_event
  - update_event
  - delete_event
  - get_sync_status
  - trigger_sync
  - raw_query
---
```

The `interface.url` is the MCP SSE endpoint. Agents that support stdio transport may alternatively spawn `agent-inbox serve --stdio` and communicate over stdin/stdout.

---

## 12. Error Codes

All error responses from the HTTP API include a machine-readable `code` field. Exhaustive list for v1:

| Code | HTTP Status | Description |
|------|------------|-------------|
| `ACCOUNT_NOT_FOUND` | 404 | No account with the given id |
| `MESSAGE_NOT_FOUND` | 404 | No message with the given id |
| `EVENT_NOT_FOUND` | 404 | No event with the given id |
| `THREAD_NOT_FOUND` | 404 | No thread with the given id |
| `CALENDAR_NOT_FOUND` | 404 | No calendar with the given id |
| `WRITE_NOT_ALLOWED` | 400 | Non-SELECT statement passed to POST /v1/query |
| `INVALID_SQL` | 400 | SQL statement failed to parse |
| `QUERY_RESULT_EMPTY` | 200 | Valid query returned zero rows (ok: true, data.rows: []) |
| `EVENT_READ_ONLY` | 403 | Attempted write to a calendar with is_read_only=1 |
| `PENDING_CHANGE_EXISTS` | 409 | A pending change for this resource already exists |
| `SYNC_NOT_RUNNING` | 503 | Sync was triggered but the daemon is not running |
| `PROVIDER_ERROR` | 502 | Provider API returned an error during pending change submission |
| `INVALID_BODY` | 422 | Request body failed validation |

---

## 13. v1 Acceptance Criteria

The following conditions must all be true for v1 to be considered complete:

| ID | Criterion |
|----|-----------|
| A1 | `chronos --add personal personal_oauth.json` completes the OAuth2 flow with `access_type=offline&prompt=consent`, writes a self-contained `~/.chronos/personal_token.json` containing `access_token`, `refresh_token`, `client_id`, `client_secret`, and `email`, and creates two rows in `accounts` |
| A2 | Running `chronos --add work personal_oauth.json` with the same credentials file (same `client_id`) but signing into a different Google account produces a separate `~/.chronos/work_token.json` with a different `email` and independent `refresh_token` |
| A3 | `chronos --add personal ...` exits with a clear error if `refresh_token` is absent from Google's response |
| A4 | `chronos --add personal ...` exits with a clear error message if the credentials JSON uses the `web` app type instead of `installed` |
| A5 | `chronos --add personal ...` prints Gmail profile (email, message count) and Calendar list count after successful token exchange |
| A6 | Incremental Gmail sync processes `messagesAdded` and `messagesDeleted` history events correctly |
| A7 | Incremental Google Calendar sync correctly uses `syncToken` and falls back to full sync on 410 |
| A8 | A recurring event master row and at least one instance row are present after a full sync of a calendar with recurring events |
| A9 | `POST /v1/query` with a cross-table SELECT joining `messages` and `events` returns correct results |
| A10 | `POST /v1/query` with a non-SELECT statement returns HTTP 400 with `code: WRITE_NOT_ALLOWED` |
| A11 | `POST /v1/events` creates a `pending_changes` row with `status='pending'` and an `events` row with `sync_state='pending'` |
| A12 | After the sync engine submits a pending event creation to Google Calendar API, the `pending_changes` row transitions to `status='confirmed'` and the `events` row transitions to `sync_state='synced'` |
| A13 | FTS5 search via `GET /v1/messages?q=...` returns results matching terms in subject and body |
| A14 | The MCP server exposes all 12 tools listed in §9.1 and each tool returns the schema defined in this document |
| A15 | `chronos --status` displays per-alias sync state, last_synced_at, and pending change count |
| A16 | `chronos --remove personal` deletes all associated data from the database and removes `~/.chronos/personal_token.json` and `~/.chronos/personal_oauth.json` |

---

## 14. Versioning

This document is **PRD v0.1** covering **Chronos v1** (Gmail + Google Calendar).

| Chronos Version | PRD Version | Providers | Key Additions |
|----------------|-------------|-----------|---------------|
| v1 | PRD v0.1 | Gmail, Google Calendar | Initial release |
| v2 | PRD v0.2 (TBD) | + Outlook, CalDAV/iCloud | Additional providers |
| v3 | PRD v0.3 (TBD) | All | LLM processing layer |

