"""Tests for the MCP server tool registration and ASGI app creation."""
from __future__ import annotations

import pytest


def test_mcp_server_creates_asgi_app():
    """Verify create_mcp_server returns an ASGI callable."""
    from chronos.mcp.server import create_mcp_server
    app = create_mcp_server(http_port=7070)
    # Should be a callable (ASGI app)
    assert callable(app)


def test_mcp_has_12_tools():
    """Verify all 12 required tools are registered."""
    from mcp.server.fastmcp import FastMCP
    from chronos.mcp import server as mcp_module

    # Re-create the MCP to inspect tool registration
    # We count by looking at the module's tool definitions
    # The tools are: list_accounts, search_messages, get_message, get_thread,
    # list_events, get_event, create_event, update_event, delete_event,
    # get_sync_status, trigger_sync, raw_query

    expected_tools = {
        "list_accounts",
        "search_messages",
        "get_message",
        "get_thread",
        "list_events",
        "get_event",
        "create_event",
        "update_event",
        "delete_event",
        "get_sync_status",
        "trigger_sync",
        "raw_query",
    }

    # Check that all tool names appear in the source
    import inspect
    source = inspect.getsource(mcp_module)
    for tool in expected_tools:
        assert f"async def {tool}" in source, f"Tool {tool} not found in MCP server"


def test_mcp_tool_signatures():
    """Verify required parameters are defined for required tools."""
    import inspect
    from chronos.mcp import server as mcp_module

    source = inspect.getsource(mcp_module)

    # create_event requires: account_id, calendar_id, title, start_unix, end_unix
    assert "account_id: str" in source
    assert "calendar_id: str" in source
    assert "start_unix: int" in source
    assert "end_unix: int" in source

    # trigger_sync requires account_id
    assert "account_id: str" in source

    # raw_query requires sql
    assert "sql: str" in source
