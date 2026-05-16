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

# Chronos — agent-inbox

Chronos is a local-first email and calendar inbox for AI agents. It syncs Gmail
and Google Calendar into a single queryable SQLite database and exposes all data
through a Model Context Protocol (MCP) server on localhost.

## Quick Start

```bash
# Install
pipx install chronos-agent-inbox

# Register an account (opens browser for OAuth2 consent)
chronos --add personal ~/path/to/credentials.json

# Start the daemon
chronos --start
```

## MCP Tools

Connect your agent to `http://127.0.0.1:7071/sse` (SSE transport) and use:

| Tool | Description |
|------|-------------|
| `list_accounts` | List all authenticated accounts and sync status |
| `search_messages` | Search emails with FTS5 full-text and structured filters |
| `get_message` | Get a single message with full HTML body |
| `get_thread` | Get all messages in a thread |
| `list_events` | List calendar events with time range and filters |
| `get_event` | Get a single calendar event |
| `create_event` | Create a calendar event (async, returns pending_change_id) |
| `update_event` | Update an existing event |
| `delete_event` | Delete an event (preserved locally until provider confirms) |
| `get_sync_status` | Get sync status for all accounts |
| `trigger_sync` | Trigger immediate sync for an account |
| `raw_query` | Execute read-only SQL SELECT against the local database |

## Key Behaviors

- **Read-only offline queries**: All reads are served from the local SQLite database.
  No provider API calls occur during agent read operations.
- **Optimistic writes**: Event creates/updates/deletes are written locally immediately
  with `sync_state='pending'` and submitted to the provider in the next sync cycle.
- **Provider-wins conflict resolution**: If a local pending change conflicts with a
  provider update, the conflict is surfaced via `get_sync_status` and the next sync
  overwrites the local row with provider data.
- **Multiple accounts**: Each Google account (personal, work, etc.) syncs independently.
  Use `account_id` filters to scope queries.

## HTTP REST API

The daemon also exposes an HTTP API at `http://127.0.0.1:7070/v1/`:

- `GET /v1/accounts`
- `GET /v1/messages`, `GET /v1/messages/:id`
- `GET /v1/threads/:id`
- `GET /v1/events`, `GET /v1/events/:id`
- `POST /v1/events`, `PATCH /v1/events/:id`, `DELETE /v1/events/:id`
- `GET /v1/calendars`
- `GET /v1/sync/status`, `POST /v1/sync/trigger`, `GET /v1/sync/conflicts`
- `POST /v1/query` (read-only SQL SELECT)

All responses use the envelope: `{"ok": true/false, "data": ..., "error": null/{code, message}}`
