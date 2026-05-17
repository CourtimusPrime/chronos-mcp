"""Tests for credential staging flow: --use / --add / --help / alias validation.

Wave 0 (RED): All tests fail until Task 2 implements the production code.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from click.testing import CliRunner

from chronos.cli.main import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_CREDS = {
    "installed": {
        "client_id": "test-client-id.apps.googleusercontent.com",
        "client_secret": "test-client-secret",
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

WEB_CREDS = {
    "web": {
        "client_id": "web-client.apps.googleusercontent.com",
        "client_secret": "web-secret",
        "redirect_uris": ["https://example.com/callback"],
    }
}


# ---------------------------------------------------------------------------
# Test 1: --use stages credentials at 0600
# ---------------------------------------------------------------------------

def test_use_stages_credentials(tmp_chronos_home, tmp_path):
    """--use with a valid Desktop-app JSON stages the file at 0600."""
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(json.dumps(VALID_CREDS))

    result = CliRunner().invoke(cli, ["--use", str(creds_file)])

    assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}\nOutput: {result.output}\nException: {result.exception}"

    staged = tmp_chronos_home / "pending_credentials.json"
    assert staged.exists(), "Staged credentials file should exist"
    assert staged.stat().st_mode & 0o777 == 0o600, "Staged file must be mode 0600"
    assert staged.read_text() == creds_file.read_text(), "Staged content must match source"


# ---------------------------------------------------------------------------
# Test 2: --use rejects web-app credentials
# ---------------------------------------------------------------------------

def test_use_rejects_web_app_credentials(tmp_chronos_home, tmp_path):
    """JSON with top-level 'web' key must be rejected with a 'Desktop app' message."""
    creds_file = tmp_path / "web_creds.json"
    creds_file.write_text(json.dumps(WEB_CREDS))

    result = CliRunner().invoke(cli, ["--use", str(creds_file)])

    assert result.exit_code != 0, "Expected non-zero exit for web-app credentials"
    combined = result.output + (str(result.exception) if result.exception else "")
    assert "Desktop app" in combined, f"Expected 'Desktop app' in output: {combined}"


# ---------------------------------------------------------------------------
# Test 3: --use rejects malformed JSON
# ---------------------------------------------------------------------------

def test_use_rejects_malformed_json(tmp_chronos_home, tmp_path):
    """A non-JSON file should produce exit != 0 and 'could not parse' in output."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json at all {{{}}")

    result = CliRunner().invoke(cli, ["--use", str(bad_file)])

    assert result.exit_code != 0, "Expected non-zero exit for malformed JSON"
    combined = result.output + (str(result.exception) if result.exception else "")
    assert "could not parse" in combined, f"Expected 'could not parse' in output: {combined}"


# ---------------------------------------------------------------------------
# Test 4: --add consumes staged credentials (Policy B: file persists)
# ---------------------------------------------------------------------------

def test_add_consumes_staged_creds(tmp_chronos_home, db_path, monkeypatch):
    """Stage creds, monkeypatch run_oauth_flow, --add alice → exit 0, staged file still exists."""
    # Stage valid credentials directly
    staged = tmp_chronos_home / "pending_credentials.json"
    staged.write_text(json.dumps(VALID_CREDS))
    staged.chmod(0o600)

    # Track call arguments
    call_args = {}

    def fake_run_oauth_flow(alias, credentials_path, conn):
        call_args["alias"] = alias
        call_args["credentials_path"] = credentials_path
        # Insert a sentinel row so the test can verify DB interaction
        from ulid import ULID
        import time
        conn.execute(
            "INSERT INTO accounts (id, provider, email, display_name, auth_type, auth_data, sync_enabled, created_at) "
            "VALUES (?, 'gmail', ?, ?, 'oauth2', '{}', 1, ?)",
            (str(ULID()), "alice@example.com", alias, int(time.time() * 1000)),
        )
        conn.commit()

    monkeypatch.setattr("chronos.cli.auth.run_oauth_flow", fake_run_oauth_flow)

    result = CliRunner().invoke(cli, ["--add", "alice", "--db-path", db_path])

    assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}\nOutput: {result.output}\nException: {result.exception}"
    assert call_args.get("alias") == "alice"
    assert call_args.get("credentials_path") == str(staged)
    # Policy B: staged file must still exist after --add
    assert staged.exists(), "Staged credentials must persist after --add (Policy B)"


# ---------------------------------------------------------------------------
# Test 5: --add without prior --use fails with actionable error
# ---------------------------------------------------------------------------

def test_add_without_use_fails(tmp_chronos_home, db_path):
    """No staged creds → non-zero exit and 'chronos --use' in output."""
    # Ensure staged file does NOT exist
    staged = tmp_chronos_home / "pending_credentials.json"
    if staged.exists():
        staged.unlink()

    result = CliRunner().invoke(cli, ["--add", "alice", "--db-path", db_path])

    assert result.exit_code != 0, "Expected non-zero exit when no staged credentials"
    combined = result.output + (str(result.exception) if result.exception else "")
    assert "chronos --use" in combined, (
        f"Expected 'chronos --use' in output to guide operator: {combined}"
    )


# ---------------------------------------------------------------------------
# Test 6: --add rejects path-traversal alias
# ---------------------------------------------------------------------------

def test_add_rejects_path_traversal_alias(tmp_chronos_home, db_path):
    """Alias '../../etc/passwd' must be rejected before any filesystem/DB operation."""
    staged = tmp_chronos_home / "pending_credentials.json"
    staged.write_text(json.dumps(VALID_CREDS))
    staged.chmod(0o600)

    result = CliRunner().invoke(cli, ["--add", "../../etc/passwd", "--db-path", db_path])

    assert result.exit_code != 0, "Expected non-zero exit for path-traversal alias"
    combined = result.output + (str(result.exception) if result.exception else "")
    assert (
        "invalid alias" in combined.lower() or
        "alias must match" in combined.lower() or
        "[A-Za-z0-9_-]" in combined
    ), f"Expected alias validation error in output: {combined}"


# ---------------------------------------------------------------------------
# Test 7: --add rejects alias with slash
# ---------------------------------------------------------------------------

def test_add_rejects_alias_with_slash(tmp_chronos_home, db_path):
    """Alias 'foo/bar' must be rejected."""
    staged = tmp_chronos_home / "pending_credentials.json"
    staged.write_text(json.dumps(VALID_CREDS))
    staged.chmod(0o600)

    result = CliRunner().invoke(cli, ["--add", "foo/bar", "--db-path", db_path])

    assert result.exit_code != 0, "Expected non-zero exit for alias with slash"
    combined = result.output + (str(result.exception) if result.exception else "")
    assert (
        "invalid alias" in combined.lower() or
        "alias must match" in combined.lower()
    ), f"Expected alias validation error in output: {combined}"


# ---------------------------------------------------------------------------
# Test 8: --help includes Google Cloud Console setup
# ---------------------------------------------------------------------------

def test_help_includes_gcp_setup():
    """--help must include Google Cloud Console Setup section with all required substrings."""
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0, f"--help should exit 0, got {result.exit_code}"

    required_substrings = [
        "Google Cloud Console Setup",
        "console.cloud.google.com",
        "Gmail API",
        "Google Calendar API",
        "OAuth client ID",
        "Desktop app",
        "chronos --use",
        "chronos --add",
    ]
    for substr in required_substrings:
        assert substr in result.output, (
            f"Expected '{substr}' in --help output.\n"
            f"Full output:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# Test 9: --add option is nargs=1 with metavar ALIAS
# ---------------------------------------------------------------------------

def test_add_option_is_nargs_one():
    """The --add Click option must be nargs=1, not nargs=2."""
    add_param = next(
        (p for p in cli.params if p.name == "add_alias"),
        None,
    )
    assert add_param is not None, "Could not find --add option (add_alias) in cli.params"
    assert add_param.nargs == 1, (
        f"Expected --add nargs==1 (single alias), got {add_param.nargs}"
    )
    assert add_param.metavar == "ALIAS", (
        f"Expected --add metavar=='ALIAS', got '{add_param.metavar}'"
    )
