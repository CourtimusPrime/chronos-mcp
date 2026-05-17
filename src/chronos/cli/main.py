"""Click CLI: --add, --remove, --list, --test, --start, --stop, --status, --sync."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.live import Live
from rich.table import Table

console = Console()


def _get_chronos_home() -> Path:
    home = os.environ.get("CHRONOS_HOME", str(Path.home() / ".chronos"))
    return Path(home)


def _get_pid_file() -> Path:
    return _get_chronos_home() / "chronos.pid"


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


@click.group(invoke_without_command=True)
@click.option("--add", "add_alias", nargs=2, metavar="ALIAS CREDENTIALS_PATH", help="Register a new account")
@click.option("--remove", "remove_alias", metavar="ALIAS", help="Remove a registered account")
@click.option("--list", "list_accounts", is_flag=True, help="List all registered accounts")
@click.option("--test", "test_alias", metavar="ALIAS", help="Test an existing account's tokens")
@click.option("--start", "start_daemon", is_flag=True, help="Start the Chronos daemon")
@click.option("--mcp-stdio", "mcp_stdio", is_flag=True, help="Start MCP server on stdio (agent subprocess mode)")
@click.option("--stop", "stop_daemon", is_flag=True, help="Stop the running daemon")
@click.option("--status", "show_status", is_flag=True, help="Show sync status")
@click.option("--sync", "sync_alias", metavar="[ALIAS]", default=None, help="Trigger sync")
@click.option("--http-port", default=None, type=int, help="HTTP API port (default: 7070)")
@click.option("--mcp-port", default=None, type=int, help="MCP server port (default: 7071)")
@click.option("--db-path", default=None, help="Override CHRONOS_DB_PATH")
@click.option("--type", "sync_type", default="incremental", type=click.Choice(["full", "incremental"]), help="Sync type")
@click.pass_context
def cli(
    ctx,
    add_alias,
    remove_alias,
    list_accounts,
    test_alias,
    start_daemon,
    mcp_stdio,
    stop_daemon,
    show_status,
    sync_alias,
    http_port,
    mcp_port,
    db_path,
    sync_type,
):
    """Chronos — agent-inbox: local-first email and calendar sync daemon."""
    if add_alias:
        alias, creds_path = add_alias
        conn = _get_conn(db_path)
        from chronos.cli.auth import run_oauth_flow
        run_oauth_flow(alias, creds_path, conn)
        conn.close()

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
        _cmd_start(http_port, mcp_port, db_path)

    elif stop_daemon:
        _cmd_stop()

    elif show_status:
        _cmd_status(db_path)

    elif sync_alias is not None:
        _cmd_sync(sync_alias if sync_alias else None, sync_type, db_path)

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


def _cmd_mcp_stdio(http_port: int | None, db_path: str | None) -> None:
    """Start MCP in stdio transport mode — full stack embedded, no external daemon needed.

    This is the subprocess entry point for agents like Hermes. It starts the sync
    engine and an internal HTTP API, then exposes the MCP interface on stdio.
    The agent communicates via stdin/stdout using the MCP protocol.
    """
    import asyncio
    _http_port = http_port or int(os.environ.get("CHRONOS_HTTP_PORT", "7072"))  # internal port
    asyncio.run(_run_mcp_stdio(db_path, _http_port))


async def _run_mcp_stdio(db_path: str | None, http_port: int) -> None:
    """Run DB + sync engine + internal HTTP API + MCP stdio concurrently."""
    import asyncio
    import uvicorn
    from chronos.db.connection import get_connection
    from chronos.db.migrations import apply_migrations
    from chronos.sync.engine import SyncEngine
    from chronos.api.app import create_app
    from chronos.mcp.server import get_mcp_instance

    conn = get_connection(db_path)
    apply_migrations(conn)
    conn.close()

    app = create_app(db_path)
    sync_engine = SyncEngine(db_path)
    mcp = get_mcp_instance(http_port)

    http_config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=http_port,
        log_level="error",   # keep stdio clean — only MCP protocol on stdout
        access_log=False,
    )
    http_server = uvicorn.Server(http_config)

    await asyncio.gather(
        http_server.serve(),
        sync_engine.run(),
        mcp.run_stdio_async(),
    )


def _cmd_start(http_port: int | None, mcp_port: int | None, db_path: str | None) -> None:
    """Start the Chronos daemon (blocks until SIGINT/SIGTERM)."""
    import asyncio
    from chronos.sync.engine import SyncEngine
    from chronos.api.app import create_app
    from chronos.mcp.server import create_mcp_server

    _http_port = http_port or int(os.environ.get("CHRONOS_HTTP_PORT", "7070"))
    _mcp_port = mcp_port or int(os.environ.get("CHRONOS_MCP_PORT", "7071"))

    # Write PID file
    pid_file = _get_pid_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    console.print(f"[bold green]Starting Chronos daemon...[/bold green]")
    console.print(f"  HTTP API:  http://127.0.0.1:{_http_port}")
    console.print(f"  MCP:       http://127.0.0.1:{_mcp_port}/sse")
    console.print("[yellow]Tip: Ctrl+C will wipe synced data. Use 'chronos --stop' for a clean shutdown.[/yellow]")

    interrupted = False
    try:
        asyncio.run(_run_daemon(db_path, _http_port, _mcp_port))
    except KeyboardInterrupt:
        interrupted = True
        console.print("\n[yellow]Ctrl+C received — wiping synced data (accounts preserved)…[/yellow]")
    finally:
        if interrupted:
            _wipe_synced_data(db_path)
        if pid_file.exists():
            pid_file.unlink()


async def _run_daemon(db_path: str | None, http_port: int, mcp_port: int) -> None:
    """Async entrypoint for the daemon."""
    import asyncio
    import uvicorn
    from chronos.db.connection import get_connection
    from chronos.db.migrations import apply_migrations
    from chronos.sync.engine import SyncEngine
    from chronos.api.app import create_app
    from chronos.mcp.server import create_mcp_server

    # Open DB and apply migrations
    conn = get_connection(db_path)
    apply_migrations(conn)

    # Create FastAPI app
    app = create_app(db_path)

    # Create MCP server
    mcp_app = create_mcp_server(http_port)

    # Create sync engine
    sync_engine = SyncEngine(db_path)

    # Configure uvicorn for HTTP API
    http_config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=http_port,
        log_level=os.environ.get("CHRONOS_LOG_LEVEL", "info").lower(),
        access_log=False,
    )
    http_server = uvicorn.Server(http_config)

    # Configure uvicorn for MCP SSE server
    mcp_config = uvicorn.Config(
        mcp_app,
        host="127.0.0.1",
        port=mcp_port,
        log_level=os.environ.get("CHRONOS_LOG_LEVEL", "info").lower(),
        access_log=False,
    )
    mcp_server = uvicorn.Server(mcp_config)

    conn.close()

    # Run all components concurrently
    await asyncio.gather(
        http_server.serve(),
        mcp_server.serve(),
        sync_engine.run(),
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
