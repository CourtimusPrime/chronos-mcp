"""Click CLI: --use, --add, --remove, --list, --test, --start, --stop, --status, --sync."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

import logging

import click
from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

console = Console()

_BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SUBTITLE = "[dim]Ctrl+C wipes data  ·  [bold]chronos --stop[/bold] for clean shutdown[/dim]"


def _state_cell(state: str | None, tick: int) -> str:
    if state in ("running", "syncing", "full_sync", "incremental_sync"):
        return f"[cyan]{_BRAILLE[tick % len(_BRAILLE)]}[/cyan] syncing"
    if state == "error":
        return "[red]✗[/red] error"
    return "[green]✓[/green] done"


def _setup_logging_for_live() -> None:
    """Silence every logger that could write to stdout/stderr while Rich Live is active.

    FastMCP's ``configure_logging`` calls ``logging.basicConfig`` with a RichHandler
    on Console(stderr=True) the first time the MCP server is created, which corrupts
    our Live region. Chronos's own sync workers also emit INFO logs that would
    bleed through. This function is idempotent — call it AFTER MCP server creation
    to win the race against FastMCP's handler install.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.CRITICAL)

    silenced = (
        "uvicorn", "uvicorn.access", "uvicorn.error", "uvicorn.asgi", "uvicorn.config",
        "chronos", "chronos.sync", "chronos.sync.engine", "chronos.sync.gmail",
        "chronos.sync.gcal", "chronos.api", "chronos.mcp", "chronos.db",
        "FastMCP", "mcp", "mcp.server", "mcp.server.fastmcp",
        "httpx", "httpcore", "googleapiclient",
    )
    for name in silenced:
        log = logging.getLogger(name)
        log.setLevel(logging.CRITICAL)
        log.handlers.clear()
        log.propagate = False


def _get_chronos_home() -> Path:
    home = os.environ.get("CHRONOS_HOME", str(Path.home() / ".chronos"))
    return Path(home)


def _get_pid_file() -> Path:
    return _get_chronos_home() / "chronos.pid"


# ---------------------------------------------------------------------------
# Credential staging constants and helpers
# ---------------------------------------------------------------------------

PENDING_CREDS_FILENAME = "pending_credentials.json"


def _pending_creds_path() -> Path:
    return _get_chronos_home() / PENDING_CREDS_FILENAME


_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_alias(alias: str) -> None:
    if not _ALIAS_PATTERN.match(alias):
        raise SystemExit(
            f"Error: invalid alias '{alias}'. Alias must match [A-Za-z0-9_-]{{1,64}}."
        )


GOOGLE_SETUP = """
\b
Google Cloud Console Setup (Web Application)
============================================
1. Visit https://console.cloud.google.com/projectcreate and create a project.
2. APIs & Services → Library: enable "Gmail API" and "Google Calendar API".
3. APIs & Services → OAuth consent screen → External:
     - Fill in app name and your email.
     - Add your Google account as a Test User (the app must stay in **Testing**
       mode — Production requires CASA verification for the Gmail scope).
     - Click "Add or remove scopes" → filter "restricted" → tick
       `https://mail.google.com/`. Also add
       `https://www.googleapis.com/auth/calendar`.
4. APIs & Services → Credentials → Create Credentials → OAuth client ID.
     - Application type: **Web application**.
     - Authorized redirect URIs → add: http://localhost:9004/callback
       (or, if you customized oauth.callback_port in config.yml, match that.)
     - Download the JSON file.
5. chronos --use /path/to/downloaded.json
6. chronos --add <alias>     (opens browser for consent)
7a. Add to .mcp.json for agents (recommended):
    { "mcpServers": { "chronos": { "command": "chronos", "args": ["--mcp-stdio"] } } }
7b. chronos --start          (daemon mode: HTTP API + background sync)

Tip: --mcp-stdio starts an embedded stack (no separate daemon needed).
     Ctrl+C while --start is running wipes synced data but keeps accounts.
     Use 'chronos --stop' from another terminal for a clean daemon shutdown.
"""


def _show_welcome() -> None:
    """Print a styled first-run welcome panel with setup steps."""
    from rich.text import Text

    steps = Text()
    steps.append("\n")
    steps.append("Step 1 — Create a Google Cloud project\n", style="bold")
    steps.append("  https://console.cloud.google.com/projectcreate\n\n", style="cyan")
    steps.append("Step 2 — Enable APIs\n", style="bold")
    steps.append("  APIs & Services → Library\n", style="dim")
    steps.append("  Enable:  Gmail API  ·  Google Calendar API\n\n")
    steps.append("Step 3 — OAuth consent screen\n", style="bold")
    steps.append("  External → fill in app name → add your Google account as a Test User\n", style="dim")
    steps.append("  Keep the app in ", style="dim")
    steps.append("Testing", style="bold yellow")
    steps.append(" mode (Production requires CASA verification)\n", style="dim")
    steps.append("  Add restricted scope ", style="dim")
    steps.append("https://mail.google.com/", style="bold")
    steps.append(" and the Calendar scope\n\n", style="dim")
    steps.append("Step 4 — Create credentials\n", style="bold")
    steps.append("  Credentials → Create Credentials → OAuth client ID\n", style="dim")
    steps.append("  Application type: ", style="dim")
    steps.append("Web application", style="bold yellow")
    steps.append("\n  Authorized redirect URI: ", style="dim")
    steps.append("http://localhost:9004/callback", style="bold")
    steps.append("\n  → Download the JSON file\n\n", style="dim")
    steps.append("Step 5 — Stage the credentials file\n", style="bold")
    steps.append("  $ chronos --use /path/to/credentials.json\n\n", style="green")
    steps.append("Step 6 — Add your account (opens browser)\n", style="bold")
    steps.append("  $ chronos --add personal\n\n", style="green")
    steps.append("Step 7 — Start the daemon\n", style="bold")
    steps.append("  $ chronos --start\n", style="green")

    console.print(Panel(
        steps,
        title="[bold green]Welcome to Chronos[/bold green]",
        subtitle="[dim]Run [bold]chronos --help[/bold] to see all options[/dim]",
        border_style="green",
        box=box.ROUNDED,
        padding=(0, 2),
    ))


def _get_conn(db_path: str | None = None):
    from chronos.db.connection import get_connection
    from chronos.db.migrations import apply_migrations

    conn = get_connection(db_path)
    apply_migrations(conn)
    return conn


def _format_last_synced(last_synced_at: int | None) -> str:
    if last_synced_at is None:
        return "never"
    elapsed = int(time.time() * 1000) - last_synced_at
    seconds = elapsed // 1000
    if seconds < 60:
        return f"{seconds} seconds ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minutes ago"
    hours = minutes // 60
    return f"{hours} hours ago"


def _progress_snapshot(sync_engine, db_path: str | None, tick: int = 0) -> Table:
    """Build a Rich Table snapshot of the current sync state for all accounts."""
    from chronos.db.connection import get_connection

    table = Table(show_header=True, header_style="bold dim", box=box.SIMPLE, padding=(0, 1))
    table.add_column("ALIAS", style="bold")
    table.add_column("PROVIDER", style="dim")
    table.add_column("STATE", min_width=10)
    table.add_column("SYNCED", justify="right")

    conn = get_connection(db_path)
    try:
        for row in conn.execute(
            "SELECT id, display_name, provider FROM accounts ORDER BY display_name, provider"
        ):
            state = sync_engine.get_worker_state(row["id"])
            count_table = "messages" if row["provider"] == "gmail" else "events"
            count = conn.execute(
                f"SELECT COUNT(*) FROM {count_table} WHERE account_id = ?",
                (row["id"],),
            ).fetchone()[0]
            suffix = "msg" if row["provider"] == "gmail" else "evt"
            table.add_row(
                row["display_name"] or "?",
                row["provider"],
                _state_cell(state, tick),
                f"{count:,} {suffix}",
            )
    finally:
        conn.close()

    return table


async def _live_progress(
    sync_engine, db_path: str | None, stop_event: asyncio.Event
) -> None:
    """Spinner → status table transition, rendered in-place via Rich Live."""
    from chronos.db.connection import get_connection

    def _account_rows() -> list[tuple[str, str, str]]:
        conn = get_connection(db_path)
        try:
            return [
                (r["id"], r["display_name"] or "?", r["provider"])
                for r in conn.execute(
                    "SELECT id, display_name, provider FROM accounts ORDER BY display_name, provider"
                )
            ]
        finally:
            conn.close()

    def _any_active(rows: list) -> bool:
        return any(
            sync_engine.get_worker_state(r_id) not in (None, "idle")
            for r_id, *_ in rows
        )

    def _startup_panel(rows: list, tick: int) -> Panel:
        ch = _BRAILLE[tick % len(_BRAILLE)]
        lines = "\n".join(
            f"  [cyan]{ch}[/cyan]  [bold]{alias:<12}[/bold] [dim]{provider}[/dim]  connecting…"
            for _, alias, provider in rows
        )
        return Panel(lines, title="[bold]Starting Chronos[/bold]",
                     subtitle=_SUBTITLE, border_style="dim", box=box.ROUNDED)

    def _sync_panel(tick: int) -> Panel:
        return Panel(
            _progress_snapshot(sync_engine, db_path, tick),
            title="[bold green]Chronos[/bold green]",
            subtitle=_SUBTITLE,
            border_style="green",
            box=box.ROUNDED,
        )

    rows = _account_rows()
    tick = 0

    with Live(console=console, refresh_per_second=4) as live:
        while not stop_event.is_set():
            if _any_active(rows):
                live.update(_sync_panel(tick))
            else:
                live.update(_startup_panel(rows, tick))
            tick += 1
            await asyncio.sleep(0.25)


def _wipe_synced_data(db_path: str | None) -> None:
    """Delete all synced rows from the six data tables and reset account cursors.

    Uses get_connection directly (not _get_conn) to skip migrations on teardown.
    Accounts and token files are preserved.
    """
    from chronos.db.connection import get_connection

    conn = get_connection(db_path)
    try:
        for table in ("messages", "threads", "events", "calendars",
                      "pending_changes", "sync_log"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE accounts SET sync_cursor = NULL, last_synced_at = NULL")
        conn.commit()
        console.print("[green]✓ Wiped synced data.[/green]")
    finally:
        conn.close()


def _cmd_use(creds_path: str) -> None:
    """Stage a credentials JSON file (Web Application OAuth client) for --add."""
    from chronos.config import get as _cfg

    src = Path(creds_path)

    try:
        data = json.loads(src.read_text())
    except (json.JSONDecodeError, ValueError):
        raise SystemExit("Error: could not parse credentials file")

    callback_port = int(_cfg("oauth.callback_port", 9004))
    callback_path = str(_cfg("oauth.callback_path", "/callback"))
    if not callback_path.startswith("/"):
        callback_path = "/" + callback_path
    redirect_uri = f"http://localhost:{callback_port}{callback_path}"

    if "installed" in data:
        raise SystemExit(
            "Desktop App credentials are no longer supported. Create a Web "
            "Application OAuth client in Google Cloud Console (Application "
            f"type: Web application), add {redirect_uri} as an Authorized "
            "redirect URI, then re-download the JSON. Run `chronos --help` "
            "for full setup instructions."
        )

    if "web" not in data:
        raise SystemExit(
            "Error: unrecognized credentials JSON shape (expected top-level "
            "'web' key from a Web Application OAuth client)."
        )

    redirect_uris = data["web"].get("redirect_uris") or []
    if redirect_uri not in redirect_uris:
        raise SystemExit(
            f"Web App credentials do not include the configured callback URL "
            f"{redirect_uri}. Add it as an Authorized redirect URI in Google "
            f"Cloud Console (APIs & Services → Credentials → your OAuth 2.0 "
            f"Client ID) and re-download the JSON."
        )

    dest = _pending_creds_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text())
    dest.chmod(0o600)

    console.print(f"[green]✓ Credentials staged at {dest}[/green]")
    console.print("Next: chronos --add <alias>")


def _cmd_add(alias: str, db_path: str | None) -> None:
    """Register a new account using staged credentials."""
    _validate_alias(alias)

    creds = _pending_creds_path()
    if not creds.exists():
        raise SystemExit(
            "Error: no staged credentials.\n"
            "Run 'chronos --use /path/to/credentials.json' first, "
            "then 'chronos --add <alias>'."
        )

    conn = _get_conn(db_path)
    try:
        from chronos.cli.auth import run_oauth_flow
        run_oauth_flow(alias, str(creds), conn)
    finally:
        conn.close()
    # Policy B: do NOT delete staged credentials — operator can run --add multiple times


@click.group(invoke_without_command=True, epilog=GOOGLE_SETUP)
@click.option("--use", "use_creds", metavar="CREDENTIALS_PATH", type=click.Path(exists=True, dir_okay=False), help="Stage a credentials JSON for the next --add")
@click.option("--add", "add_alias", metavar="ALIAS", help="Register a new account (requires prior --use)")
@click.option("--remove", "remove_alias", metavar="ALIAS", help="Remove a registered account")
@click.option("--list", "list_accounts", is_flag=True, help="List all registered accounts")
@click.option("--test", "test_alias", metavar="ALIAS", help="Test an existing account's tokens")
@click.option("--start", "start_daemon", is_flag=True, help="Start the Chronos daemon")
@click.option("--walkthrough", "show_walkthrough", is_flag=True, help="Print step-by-step Google Cloud + Chronos setup guide")
@click.option("--mcp", "install_mcp", is_flag=True, help="Add Chronos to an .mcp.json file (prompts for project or global scope)")
@click.option("--mcp-stdio", "mcp_stdio", is_flag=True, help="Start MCP server on stdio — embedded stack, no daemon needed (use in .mcp.json)")
@click.option("--stop", "stop_daemon", is_flag=True, help="Stop the running daemon")
@click.option("--status", "show_status", is_flag=True, help="Show sync status")
@click.option("--sync", "sync_alias", metavar="[ALIAS]", default=None, help="Trigger sync")
@click.option("--http-port", default=None, type=int, help="HTTP API port (default: 7070)")
@click.option("--db-path", default=None, help="Override CHRONOS_DB_PATH")
@click.option("--type", "sync_type", default="incremental", type=click.Choice(["full", "incremental"]), help="Sync type")
@click.pass_context
def cli(
    ctx,
    use_creds,
    add_alias,
    remove_alias,
    list_accounts,
    test_alias,
    start_daemon,
    show_walkthrough,
    install_mcp,
    mcp_stdio,
    stop_daemon,
    show_status,
    sync_alias,
    http_port,
    db_path,
    sync_type,
):
    """Chronos — agent-inbox: local-first email and calendar sync daemon."""
    # Materialize ~/.chronos/config.yml from the bundled template on first run.
    # Idempotent — won't overwrite an existing user config.
    from chronos.config import ensure_user_config, reset_cache as _reset_cfg
    if ensure_user_config() is not None:
        _reset_cfg()

    if use_creds:
        _cmd_use(use_creds)
        return

    if add_alias:
        _cmd_add(add_alias, db_path)
        return

    elif show_walkthrough:
        _cmd_walkthrough()

    elif install_mcp:
        _cmd_mcp_install()

    elif mcp_stdio:
        _cmd_mcp_stdio(http_port, db_path)

    elif remove_alias:
        _cmd_remove(remove_alias, db_path)

    elif list_accounts:
        _cmd_list(db_path)

    elif test_alias:
        conn = _get_conn(db_path)
        from chronos.cli.auth import test_account
        test_account(test_alias, conn)
        conn.close()

    elif start_daemon:
        _cmd_start(http_port, db_path)

    elif stop_daemon:
        _cmd_stop()

    elif show_status:
        _cmd_status(db_path)

    elif sync_alias is not None:
        _cmd_sync(sync_alias if sync_alias else None, sync_type, db_path)

    else:
        # First-run: no accounts configured → show guided setup panel
        try:
            conn = _get_conn(db_path)
            has_accounts = conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None
            conn.close()
        except Exception:
            has_accounts = False
        if not has_accounts:
            _show_welcome()
        else:
            click.echo(ctx.get_help())


def _cmd_remove(alias: str, db_path: str | None) -> None:
    """Remove an alias: delete accounts rows + cascade + token file."""
    conn = _get_conn(db_path)

    rows = conn.execute(
        "SELECT * FROM accounts WHERE display_name = ?", (alias,)
    ).fetchall()

    if not rows:
        console.print(f"[red]Error: alias '{alias}' not found.[/red]")
        conn.close()
        sys.exit(1)

    # Delete all accounts for this alias (cascade handles related rows)
    conn.execute("DELETE FROM accounts WHERE display_name = ?", (alias,))
    conn.commit()
    conn.close()

    # Remove token file
    token_file = _get_chronos_home() / f"{alias}_token.json"
    if token_file.exists():
        token_file.unlink()
        console.print(f"Removed token file: {token_file}")

    console.print(f"[green]✓ Removed account: {alias}[/green]")


def _cmd_list(db_path: str | None) -> None:
    """List all registered accounts."""
    conn = _get_conn(db_path)
    rows = conn.execute(
        "SELECT display_name, provider, email, last_synced_at FROM accounts ORDER BY display_name, provider"
    ).fetchall()
    conn.close()

    if not rows:
        console.print("[dim]No accounts registered. Use 'chronos --add' to register an account.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("ALIAS")
    table.add_column("PROVIDER")
    table.add_column("EMAIL")
    table.add_column("LAST SYNCED")

    for row in rows:
        table.add_row(
            row["display_name"] or "",
            row["provider"],
            row["email"],
            _format_last_synced(row["last_synced_at"]),
        )

    console.print(table)


_WALKTHROUGH_MD = """\
# Chronos Setup Walkthrough

## Part 1 — Google Cloud Console

### Create a project

1. Visit https://console.cloud.google.com and sign in with your preferred Google account.
2. Click the project selector (the box next to the "Google Cloud" logo) → **New Project**.
3. Give it a name (e.g. "Chronos MCP") and click **Create**.
4. After creation, click the project selector again and select your new project.

### Enable APIs

5. Open the hamburger menu (☰) → **APIs & Services**.
6. Click **Enable APIs and services**.
7. Search for **Google Calendar API** → select it → click **Enable**.
8. Press the back arrow (←) to return to the library.
9. Search for **Gmail API** → select it → click **Enable**.

### Configure the OAuth consent screen

10. Open the sidebar → **APIs & Services** → **Credentials**.
11. Click **Configure consent screen** → **Get started**.
12. Enter an app name (e.g. "Chronos MCP") and choose your email under **User support email** → **Next**.
13. Select **External** → **Next**.
14. Enter a contact email address → **Next**.
15. Tick **I agree to the Google API Services: User Data Policy** → **Continue** → **Create**.
16. On the redirect screen, click **Create OAuth client**.

### Create OAuth credentials

17. From the **Application type** dropdown select **Web application**.
18. Under **Authorized redirect URIs** click **Add URI** and enter:

    ```
    http://localhost:9004/callback
    ```

19. Click **Create**.
20. In the **OAuth client created** modal, click **Download JSON**. Rename the file to something easy like `chronos_creds.json`.
21. Click **OK** to close the modal.

### Add API scopes

22. From the sidebar select **Data Access** → **Add or remove scopes**.
23. In the search field enter: `https://www.googleapis.com/auth/calendar` → select the item that appears.
24. Clear the field and enter: `https://mail.google.com` → select the item that appears.
25. Click **Update** → **Save**.

### Add test users

26. From the sidebar select **Audience** → **Add users** (under Test users).
27. Enter the email address(es) of the Google account(s) you want to connect.
28. Click **Save**.

---

## Part 2 — Terminal Setup

### Stage credentials and add accounts

```zsh
# Stage the credentials file you downloaded in step 20
chronos --use ~/Downloads/chronos_creds.json

# Add an account (opens browser for OAuth consent)
# The alias must correspond to one of the accounts you added in step 27
chronos --add personal

# Repeat --add for any additional accounts
chronos --add work
```

When prompted by Google, if you see **"Google hasn't verified this app"** click **Continue**.
Click **Select all** → **Continue** to grant all required scopes.

### Start syncing

```zsh
chronos --start
```

Chronos will download all your emails and calendar events into a local SQLite database.
Your AI agents query this database — no live Google API calls at read time.

### Add the MCP server to Claude Code

In a second terminal:

```zsh
chronos --mcp
```

This writes the `chronos --mcp-stdio` entry into your `.mcp.json` file and adds
instructions to `~/.claude/CLAUDE.md` so Claude uses Chronos for email/calendar queries.

### Test it

```zsh
# Quick test
claude "What's the most recent email I received?"

# With full tool output
claude --dangerously-skip-permissions --verbose "What's the most recent email I received?"
```

---

> **Ctrl+C vs --stop:** Pressing Ctrl+C while `chronos --start` is running wipes synced
> data (emails, events) but preserves accounts. Use `chronos --stop` for a clean shutdown.
>
> Run `chronos --walkthrough` at any time to reprint this guide.
"""


def _cmd_walkthrough() -> None:
    """Print the full setup walkthrough and save WALKTHROUGH.md to ~/.chronos/."""
    from rich.markdown import Markdown
    from rich.rule import Rule

    console.print()
    console.print(Rule("[bold green]Chronos Setup Walkthrough[/bold green]", style="green"))
    console.print()
    console.print(Markdown(_WALKTHROUGH_MD))
    console.print()
    console.print(Rule(style="dim"))

    # Write WALKTHROUGH.md to ~/.chronos/ for future reference
    dest = _get_chronos_home() / "WALKTHROUGH.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_WALKTHROUGH_MD)
    console.print(f"[dim]Saved to {dest}[/dim]")
    console.print()


def _cmd_mcp_install() -> None:
    """Prompt for scope and write the chronos stdio entry into .mcp.json."""
    scope = click.prompt(
        "Scope",
        type=click.Choice(["project", "global"], case_sensitive=False),
        default="project",
    )

    if scope.lower() == "project":
        target = Path.cwd() / ".mcp.json"
    else:
        target = Path.home() / ".claude" / ".mcp.json"

    existing: dict = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text())
        except json.JSONDecodeError:
            console.print(f"[yellow]Warning: {target} contains invalid JSON — overwriting.[/yellow]")

    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["chronos"] = {
        "command": "chronos",
        "args": ["--mcp-stdio"],
    }

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(existing, indent=2) + "\n")

    console.print(f"[green]✓ Added chronos to {target}[/green]")

    _ensure_claude_md_instructions()

    console.print("  [dim]Restart your MCP client (Claude Code, Cursor, …) to pick up the change.[/dim]")


_CLAUDE_MD_SECTION = """\
## Chronos MCP (email & calendar)

When the user asks questions about their Gmail or Google Calendar — including reading emails,
checking events, searching threads, or anything related to their inbox or schedule — use the
Chronos MCP tools instead of asking the user to look it up themselves.

When the user asks to:
- Add, update, or delete calendar events → use Chronos MCP calendar tools
- Compose, send, draft, or reply to emails → use Chronos MCP email tools
- Search or summarize their inbox → use Chronos MCP email tools
"""

_CLAUDE_MD_MARKER = "## Chronos MCP (email & calendar)"


def _ensure_claude_md_instructions() -> None:
    """Append Chronos usage instructions to ~/.claude/CLAUDE.md if not already present."""
    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True, exist_ok=True)

    existing = claude_md.read_text() if claude_md.exists() else ""
    if _CLAUDE_MD_MARKER in existing:
        console.print("  [dim]~/.claude/CLAUDE.md already has Chronos instructions.[/dim]")
        return

    with claude_md.open("a") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write("\n" + _CLAUDE_MD_SECTION)

    console.print(f"[green]✓ Added Chronos instructions to {claude_md}[/green]")


def _cmd_mcp_stdio(http_port: int | None, db_path: str | None) -> None:
    """Start MCP in stdio transport mode — full stack embedded, no external daemon needed.

    This is the subprocess entry point for agents like Hermes. It starts the sync
    engine and an internal HTTP API, then exposes the MCP interface on stdio.
    The agent communicates via stdin/stdout using the MCP protocol.
    """
    import asyncio
    asyncio.run(_run_mcp_stdio(db_path, http_port))


def _daemon_port() -> int:
    """Return the configured daemon HTTP port."""
    from chronos.config import get as _cfg
    return int(os.environ.get("CHRONOS_HTTP_PORT", _cfg("network.http_port", 7070)))


def _daemon_running(port: int) -> bool:
    """Return True if a Chronos HTTP daemon is already listening on *port*."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _free_port() -> int:
    """Ask the OS for an available ephemeral port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _run_mcp_stdio(db_path: str | None, http_port: int | None) -> None:
    """Run DB + sync engine + internal HTTP API + MCP stdio concurrently.

    If the Chronos daemon is already running, attach to it rather than
    starting a redundant embedded stack. This avoids port conflicts when
    multiple Claude Code sessions are open simultaneously.
    """
    import asyncio
    import uvicorn
    from chronos.mcp.server import get_mcp_instance

    daemon_port = http_port or _daemon_port()

    if _daemon_running(daemon_port):
        # Daemon is live — just wire the MCP layer to it.
        mcp = get_mcp_instance(daemon_port)
        await mcp.run_stdio_async()
        return

    # No daemon — start the full embedded stack on a free port.
    from chronos.db.connection import get_connection
    from chronos.db.migrations import apply_migrations
    from chronos.sync.engine import SyncEngine
    from chronos.api.app import create_app

    embedded_port = http_port or _free_port()

    conn = get_connection(db_path)
    apply_migrations(conn)
    conn.close()

    app = create_app(db_path)
    sync_engine = SyncEngine(db_path)
    mcp = get_mcp_instance(embedded_port)

    http_config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=embedded_port,
        log_level="error",
        access_log=False,
        log_config=None,
    )
    http_server = uvicorn.Server(http_config)

    await asyncio.gather(
        http_server.serve(),
        sync_engine.run(),
        mcp.run_stdio_async(),
    )


def _cmd_start(http_port: int | None, db_path: str | None) -> None:
    """Start the Chronos daemon (blocks until SIGINT/SIGTERM)."""
    import asyncio
    from chronos.config import get as _cfg
    _http_port = http_port or int(os.environ.get("CHRONOS_HTTP_PORT", _cfg("network.http_port", 7070)))

    # Write PID file
    pid_file = _get_pid_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    console.print(f"[bold green]Starting Chronos daemon...[/bold green]")
    console.print(f"  HTTP API:  http://127.0.0.1:{_http_port}")
    console.print(f"  MCP (stdio): chronos --mcp-stdio  [dim](use in .mcp.json for Claude Code)[/dim]")
    console.print("[yellow]Tip: Ctrl+C will wipe synced data. Use 'chronos --stop' for a clean shutdown.[/yellow]")

    interrupted = False
    try:
        asyncio.run(_run_daemon(db_path, _http_port))
    except KeyboardInterrupt:
        interrupted = True
        console.print("\n[yellow]Ctrl+C received — wiping synced data (accounts preserved)…[/yellow]")
    finally:
        if interrupted:
            _wipe_synced_data(db_path)
        if pid_file.exists():
            pid_file.unlink()


async def _run_daemon(db_path: str | None, http_port: int) -> None:
    """Async entrypoint for the daemon."""
    import uvicorn
    from chronos.db.connection import get_connection
    from chronos.db.migrations import apply_migrations
    from chronos.sync.engine import SyncEngine
    from chronos.api.app import create_app

    # Open DB and apply migrations
    conn = get_connection(db_path)
    apply_migrations(conn)

    # Create FastAPI app
    app = create_app(db_path)

    # Create sync engine
    sync_engine = SyncEngine(db_path)

    _setup_logging_for_live()

    # stop_event used by _live_progress to exit on orderly shutdown
    stop_event = asyncio.Event()

    # Configure uvicorn for HTTP API
    http_config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=http_port,
        log_level="critical",
        access_log=False,
        log_config=None,
    )
    http_server = uvicorn.Server(http_config)

    conn.close()

    # Run all components concurrently
    await asyncio.gather(
        http_server.serve(),
        sync_engine.run(),
        _live_progress(sync_engine, db_path, stop_event),
    )


def _cmd_stop() -> None:
    """Stop the running daemon."""
    pid_file = _get_pid_file()

    if not pid_file.exists():
        console.print("[yellow]Daemon is not running.[/yellow]")
        return

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Sent SIGTERM to daemon (PID {pid}).[/green]")
    except (ValueError, ProcessLookupError):
        console.print("[yellow]Daemon process not found. Removing stale PID file.[/yellow]")
        pid_file.unlink(missing_ok=True)
    except PermissionError:
        console.print("[red]Permission denied sending signal to daemon.[/red]")
        sys.exit(1)


def _cmd_status(db_path: str | None) -> None:
    """Show sync status for all accounts."""
    # Try to contact the running daemon first
    http_port = int(os.environ.get("CHRONOS_HTTP_PORT", "7070"))

    try:
        import httpx
        response = httpx.get(f"http://127.0.0.1:{http_port}/v1/sync/status", timeout=2.0)
        if response.status_code == 200:
            data = response.json().get("data", {})
            accounts = data.get("accounts", [])
            if not accounts:
                console.print("[dim]No accounts registered.[/dim]")
                return

            table = Table(show_header=True, header_style="bold")
            table.add_column("ACCOUNT")
            table.add_column("PROVIDER")
            table.add_column("STATE")
            table.add_column("LAST SYNCED")
            table.add_column("PENDING")
            table.add_column("CONFLICTS")

            for acct in accounts:
                table.add_row(
                    acct.get("email", ""),
                    acct.get("provider", ""),
                    acct.get("sync_state", ""),
                    _format_last_synced(acct.get("last_synced_at")),
                    str(acct.get("pending_changes_count", 0)),
                    str(acct.get("conflict_count", 0)),
                )

            console.print(table)
            return
    except Exception:
        pass

    # Daemon not running — read from DB directly
    pid_file = _get_pid_file()
    if not pid_file.exists():
        console.print("Daemon is not running. Start it with: [bold]chronos --start[/bold]")
    else:
        console.print("[yellow]Daemon appears to be starting or stopped unexpectedly.[/yellow]")

    # Still show local DB state
    try:
        conn = _get_conn(db_path)
        rows = conn.execute(
            "SELECT display_name, provider, email, last_synced_at FROM accounts"
        ).fetchall()
        conn.close()

        if rows:
            table = Table(show_header=True, header_style="bold")
            table.add_column("ALIAS")
            table.add_column("PROVIDER")
            table.add_column("EMAIL")
            table.add_column("LAST SYNCED")
            for row in rows:
                table.add_row(
                    row["display_name"] or "",
                    row["provider"],
                    row["email"],
                    _format_last_synced(row["last_synced_at"]),
                )
            console.print(table)
    except Exception:
        pass


def _cmd_sync(alias: str | None, sync_type: str, db_path: str | None) -> None:
    """Trigger sync for alias or all accounts."""
    http_port = int(os.environ.get("CHRONOS_HTTP_PORT", "7070"))

    # Try daemon first
    try:
        import httpx
        conn = _get_conn(db_path)

        if alias:
            rows = conn.execute(
                "SELECT id FROM accounts WHERE display_name = ?", (alias,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT id FROM accounts").fetchall()

        conn.close()

        for row in rows:
            try:
                response = httpx.post(
                    f"http://127.0.0.1:{http_port}/v1/sync/trigger",
                    json={"account_id": row["id"], "type": sync_type},
                    timeout=5.0,
                )
                if response.status_code == 200:
                    console.print(f"[green]Triggered {sync_type} sync for account {row['id']}[/green]")
            except Exception:
                console.print(f"[yellow]Daemon not available; sync will run on next startup.[/yellow]")
                break

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)
