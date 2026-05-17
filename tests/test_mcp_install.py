"""Tests for chronos --mcp: writes chronos stdio entry into .mcp.json."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from chronos.cli.main import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke(input_text: str, cwd: Path | None = None) -> object:
    runner = CliRunner()
    kwargs = {}
    if cwd is not None:
        kwargs["catch_exceptions"] = False
    with runner.isolated_filesystem(temp_dir=cwd) if cwd is None else _noop():
        return runner.invoke(cli, ["--mcp"], input=input_text, catch_exceptions=False)


class _noop:
    """No-op context manager so we can optionally skip isolated_filesystem."""
    def __enter__(self): return self
    def __exit__(self, *_): pass


# ---------------------------------------------------------------------------
# Test 1: project scope writes .mcp.json in cwd
# ---------------------------------------------------------------------------

def test_mcp_project_scope_writes_in_cwd(tmp_path, monkeypatch, tmp_chronos_home):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["--mcp"], input="project\n", catch_exceptions=False)

    assert result.exit_code == 0, result.output
    target = tmp_path / ".mcp.json"
    assert target.exists(), f".mcp.json not created at {target}"

    data = json.loads(target.read_text())
    assert data["mcpServers"]["chronos"] == {
        "command": "chronos",
        "args": ["--mcp-stdio"],
    }
    assert "✓" in result.output or "Added" in result.output


# ---------------------------------------------------------------------------
# Test 2: global scope writes ~/.claude/.mcp.json
# ---------------------------------------------------------------------------

def test_mcp_global_scope_writes_to_claude_home(tmp_path, monkeypatch, tmp_chronos_home):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # Patch Path.home() by monkeypatching the module-level Path
    import chronos.cli.main as m
    monkeypatch.setattr(m, "Path", lambda *a, **kw: _PatchedPath(fake_home, *a, **kw))

    # Use a simpler approach: just patch Path.home directly
    monkeypatch.undo()  # undo the above
    monkeypatch.setenv("HOME", str(fake_home))

    result = CliRunner().invoke(cli, ["--mcp"], input="global\n", catch_exceptions=False)

    assert result.exit_code == 0, result.output
    target = fake_home / ".claude" / ".mcp.json"
    assert target.exists(), f"Expected {target} to exist"
    data = json.loads(target.read_text())
    assert data["mcpServers"]["chronos"]["command"] == "chronos"


# ---------------------------------------------------------------------------
# Test 3: merges with existing .mcp.json — other servers preserved
# ---------------------------------------------------------------------------

def test_mcp_project_merges_existing(tmp_path, monkeypatch, tmp_chronos_home):
    monkeypatch.chdir(tmp_path)
    existing = {
        "mcpServers": {
            "other-tool": {"command": "other", "args": []},
        }
    }
    target = tmp_path / ".mcp.json"
    target.write_text(json.dumps(existing))

    result = CliRunner().invoke(cli, ["--mcp"], input="project\n", catch_exceptions=False)

    assert result.exit_code == 0, result.output
    data = json.loads(target.read_text())
    assert "other-tool" in data["mcpServers"], "Existing server must be preserved"
    assert data["mcpServers"]["chronos"]["command"] == "chronos"


# ---------------------------------------------------------------------------
# Test 4: overwrites existing chronos entry cleanly
# ---------------------------------------------------------------------------

def test_mcp_project_overwrites_existing_chronos(tmp_path, monkeypatch, tmp_chronos_home):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".mcp.json"
    target.write_text(json.dumps({
        "mcpServers": {
            "chronos": {"command": "old-command", "args": ["--old-flag"]},
        }
    }))

    CliRunner().invoke(cli, ["--mcp"], input="project\n", catch_exceptions=False)

    data = json.loads(target.read_text())
    assert data["mcpServers"]["chronos"]["command"] == "chronos"
    assert data["mcpServers"]["chronos"]["args"] == ["--mcp-stdio"]


# ---------------------------------------------------------------------------
# Test 5: handles invalid JSON in existing file gracefully
# ---------------------------------------------------------------------------

def test_mcp_project_handles_invalid_json(tmp_path, monkeypatch, tmp_chronos_home):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".mcp.json"
    target.write_text("not valid json {{{")

    result = CliRunner().invoke(cli, ["--mcp"], input="project\n", catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "Warning" in result.output or "invalid" in result.output.lower()
    data = json.loads(target.read_text())
    assert data["mcpServers"]["chronos"]["command"] == "chronos"


# ---------------------------------------------------------------------------
# Test 6: creates parent directory if it doesn't exist (global scope)
# ---------------------------------------------------------------------------

def test_mcp_global_creates_parent_dir(tmp_path, monkeypatch, tmp_chronos_home):
    fake_home = tmp_path / "newhome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # ~/.claude does NOT exist yet

    result = CliRunner().invoke(cli, ["--mcp"], input="global\n", catch_exceptions=False)

    assert result.exit_code == 0, result.output
    target = fake_home / ".claude" / ".mcp.json"
    assert target.exists(), f"Expected {target} to be created including parent dir"


# ---------------------------------------------------------------------------
# Test 7: output tells user to restart their MCP client
# ---------------------------------------------------------------------------

def test_mcp_output_mentions_restart(tmp_path, monkeypatch, tmp_chronos_home):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["--mcp"], input="project\n", catch_exceptions=False)

    assert result.exit_code == 0
    assert "Restart" in result.output or "restart" in result.output


# ---------------------------------------------------------------------------
# Test 8: written JSON is valid and pretty-printed (has newline at end)
# ---------------------------------------------------------------------------

def test_mcp_written_json_is_valid(tmp_path, monkeypatch, tmp_chronos_home):
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(cli, ["--mcp"], input="project\n", catch_exceptions=False)

    raw = (tmp_path / ".mcp.json").read_text()
    assert raw.endswith("\n"), "File should end with a newline"
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
