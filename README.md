# Chronos — agent-inbox

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Local-first email and calendar inbox for AI agents. Syncs Gmail and Google Calendar
into a single queryable SQLite database and exposes all data through an MCP server.

## Features

- Sync multiple Gmail and Google Calendar accounts into one SQLite database
- Full-text search via FTS5 with BM25 relevance ranking (messages, events, attachment names)
- MCP server via `chronos --mcp-stdio` (stdio transport, no port required)
- HTTP REST API at `localhost:7070` for direct queries
- Read-only SQL interface via `POST /v1/query`
- Optimistic event writes with provider-wins conflict resolution
- Incremental sync with historyId (Gmail) and syncToken (Calendar)
- ULID primary keys, WAL mode, no cloud dependencies at query time

## Installation

```bash
pipx install chronos-mcp
```

Or from source:

```bash
git clone https://github.com/CourtimusPrime/chronos.git
cd chronos
pip install -e .
```

## Setup

Run the interactive walkthrough for detailed step-by-step instructions:

```bash
chronos --walkthrough
```

This prints the full guide in the terminal and saves it to `~/.chronos/WALKTHROUGH.md` for future reference.

### Quick start

**1. Google Cloud Console**

- Create a project at [console.cloud.google.com](https://console.cloud.google.com)
- Enable **Gmail API** and **Google Calendar API**
- Configure OAuth consent screen (External, Testing mode) — add your Google account as a Test User
- Add scopes: `https://mail.google.com/` and `https://www.googleapis.com/auth/calendar`
- Create an OAuth client ID — **Application type: Web application**
- Add authorized redirect URI: `http://localhost:9004/callback`
- Download the credentials JSON

> Desktop App credentials (`"installed"` JSON shape) are **not supported** —
> Chronos uses the `https://mail.google.com/` restricted scope, which requires
> a Web Application OAuth client in Testing mode.

**2. Register accounts**

```bash
# Stage your credentials file
chronos --use /path/to/credentials.json

# Add accounts (opens browser for OAuth consent)
chronos --add personal
chronos --add work
```

**3. Start syncing**

```bash
chronos --start
```

Chronos downloads all your emails and calendar events into a local SQLite database.
Your AI agents query this database — no live Google API calls at read time.

**4. Add the MCP server**

```bash
chronos --mcp
```

Writes the stdio entry into `.mcp.json` and appends instructions to `~/.claude/CLAUDE.md`.

**5. Test it**

```bash
claude "What's the most recent email I received?"
```

## CLI Reference

```
chronos --use CREDENTIALS_PATH          # Stage a credentials JSON for --add
chronos --add ALIAS                     # Register a new account (requires prior --use)
chronos --remove ALIAS                  # Remove an account and its data
chronos --list                          # List all registered accounts
chronos --test ALIAS                    # Test account tokens
chronos --start [--http-port N]         # Start daemon (HTTP API + background sync)
chronos --stop                          # Stop the running daemon (preserves synced data)
chronos --status                        # Show sync status
chronos --sync [ALIAS] [--type full|incremental]  # Trigger sync
chronos --mcp                           # Add Chronos to .mcp.json (prompts for scope)
chronos --mcp-stdio                     # Start embedded MCP server on stdio
chronos --walkthrough                   # Print full setup guide
```

> **Ctrl+C vs --stop:** Pressing Ctrl+C while `chronos --start` is running wipes
> synced data (emails, threads, events, calendars) but preserves accounts.
> Use `chronos --stop` from another terminal for a clean shutdown that preserves data.

## Environment Variables

| Variable            | Default                    | Description                        |
| ------------------- | -------------------------- | ---------------------------------- |
| `CHRONOS_HOME`      | `~/.chronos`               | Credentials and database directory |
| `CHRONOS_DB_PATH`   | `$CHRONOS_HOME/chronos.db` | SQLite database file               |
| `CHRONOS_HTTP_PORT` | `7070`                     | HTTP API port                      |
| `CHRONOS_LOG_LEVEL` | `INFO`                     | Log level                          |

## Configuration

`config.yml` is auto-created at `~/.chronos/config.yml` on first run. The OAuth
callback port is the most commonly customized setting:

```yaml
settings:
  oauth:
    callback_port: 9004     # local server bind during `chronos --add`
    callback_path: /callback # appended to the redirect URI
```

If you change `callback_port`, update the **Authorized redirect URI** in Google Cloud Console to match.

## License

MIT
