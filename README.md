# Chronos — agent-inbox

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Local-first email and calendar inbox for AI agents. Syncs Gmail and Google Calendar
into a single queryable SQLite database and exposes all data through an MCP server.

## Features

- Sync multiple Gmail and Google Calendar accounts into one SQLite database
- Full-text search via FTS5 (messages and events)
- MCP server at `localhost:7071/sse` for agent integration
- HTTP REST API at `localhost:7070` for direct queries
- Read-only SQL interface via `POST /v1/query`
- Optimistic event writes with provider-wins conflict resolution
- Incremental sync with historyId (Gmail) and syncToken (Calendar)
- ULID primary keys, WAL mode, no cloud dependencies at query time

## Installation

```bash
pipx install chronos-agent-inbox
```

Or from source:

```bash
git clone https://github.com/CourtimusPrime/chronos.git
cd chronos
pip install -e .
```

## Setup

### Prerequisites

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project and enable **Gmail API** and **Google Calendar API**
3. Create an OAuth client ID (Desktop app type)
4. Download the credentials JSON file

### Register an account

> Run `chronos --help` for full Google Cloud Console setup instructions.

```bash
# Step 1: Stage your credentials (Desktop app OAuth JSON from Google Cloud Console)
chronos --use /path/to/credentials.json

# Step 2: Register an account (opens browser for OAuth2 consent)
chronos --add personal
```

Staged credentials persist until the next `--use` call, so you can register
multiple accounts with one credentials file:

```bash
chronos --use /path/to/credentials.json
chronos --add personal
chronos --add work
```

The `--add` step:

- Writes `~/.chronos/personal_token.json` (self-contained token file)
- Creates two rows in the accounts table (gmail + google_calendar)
- Prints a confirmation summary

### Start syncing

```bash
chronos --start
```

The daemon starts on `127.0.0.1:7070` (HTTP) and `127.0.0.1:7071` (MCP/SSE).

## CLI Reference

```
chronos --use CREDENTIALS_PATH          # Stage a credentials JSON for --add
chronos --add ALIAS                     # Register a new account (requires prior --use)
chronos --remove ALIAS                  # Remove an account and its data
chronos --list                          # List all registered accounts
chronos --test ALIAS                    # Test account tokens
chronos --start [--http-port N] [--mcp-port N]  # Start daemon
chronos --stop                          # Stop the running daemon (preserves synced data)
chronos --status                        # Show sync status
chronos --sync [ALIAS] [--type full|incremental]  # Trigger sync
```

> **Ctrl+C vs --stop:** Pressing Ctrl+C while `chronos --start` is running wipes
> synced data (emails, threads, events, calendars) but preserves accounts.
> Use `chronos --stop` from another terminal for a clean shutdown that preserves data.
>
> Run `chronos --help` for Google Cloud Console setup steps.

## Environment Variables

| Variable            | Default                    | Description                        |
| ------------------- | -------------------------- | ---------------------------------- |
| `CHRONOS_HOME`      | `~/.chronos`               | Credentials and database directory |
| `CHRONOS_DB_PATH`   | `$CHRONOS_HOME/chronos.db` | SQLite database file               |
| `CHRONOS_HTTP_PORT` | `7070`                     | HTTP API port                      |
| `CHRONOS_MCP_PORT`  | `7071`                     | MCP server port                    |
| `CHRONOS_LOG_LEVEL` | `INFO`                     | Log level                          |

## License

MIT
