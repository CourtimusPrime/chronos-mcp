"""Tests for credential staging flow: --use / --add / --help / alias validation.

Updated in Phase 05.2: Web Application OAuth client is now the only accepted
credentials shape; Desktop App ('installed') JSON is rejected with a migration
pointer.
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

WEB_CREDS = {
    "web": {
        "client_id": "web-client.apps.googleusercontent.com",
        "client_secret": "web-secret",
        "redirect_uris": ["http://localhost:9004/callback"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

DESKTOP_CREDS = {
    "installed": {
        "client_id": "test-client-id.apps.googleusercontent.com",
        "client_secret": "test-client-secret",
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Drop the lru_cache on chronos.config.load_config between tests so that
    per-test CHRONOS_CONFIG overrides take effect."""
    from chronos.config import reset_cache
    reset_cache()
    yield
    reset_cache()


# ---------------------------------------------------------------------------
# Test 1: --use stages credentials at 0600 (Web Application JSON)
# ---------------------------------------------------------------------------

def test_use_stages_credentials(tmp_chronos_home, tmp_path):
    """--use with a valid Web Application JSON stages the file at 0600."""
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(json.dumps(WEB_CREDS))

    result = CliRunner().invoke(cli, ["--use", str(creds_file)])

    assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}\nOutput: {result.output}\nException: {result.exception}"

    staged = tmp_chronos_home / "pending_credentials.json"
    assert staged.exists(), "Staged credentials file should exist"
    assert staged.stat().st_mode & 0o777 == 0o600, "Staged file must be mode 0600"
    assert staged.read_text() == creds_file.read_text(), "Staged content must match source"


# ---------------------------------------------------------------------------
# Test 2: --use rejects Desktop App credentials (inverted from pre-05.2)
# ---------------------------------------------------------------------------

def test_use_rejects_desktop_credentials(tmp_chronos_home, tmp_path):
    """JSON with top-level 'installed' key must be rejected with a Web-App migration pointer."""
    creds_file = tmp_path / "desktop_creds.json"
    creds_file.write_text(json.dumps(DESKTOP_CREDS))

    result = CliRunner().invoke(cli, ["--use", str(creds_file)])

    assert result.exit_code != 0, "Expected non-zero exit for Desktop App credentials"
    combined = result.output + (str(result.exception) if result.exception else "")
    assert "Desktop App credentials are no longer supported" in combined, (
        f"Expected migration pointer in output: {combined}"
    )
    assert "Web application" in combined, (
        f"Expected 'Web application' in output: {combined}"
    )


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
    # Stage valid Web App credentials directly
    staged = tmp_chronos_home / "pending_credentials.json"
    staged.write_text(json.dumps(WEB_CREDS))
    staged.chmod(0o600)

    # Track call arguments
    call_args = {}

    def fake_run_oauth_flow(alias, credentials_path, conn):
        call_args["alias"] = alias
        call_args["credentials_path"] = credentials_path
        # Verify the staged file parses as Web App JSON (new shape)
        with open(credentials_path) as fh:
            payload = json.load(fh)
        assert "web" in payload, f"run_oauth_flow expected creds['web'], got keys {list(payload)}"
        assert payload["web"]["client_id"].endswith(".apps.googleusercontent.com")
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
    staged.write_text(json.dumps(WEB_CREDS))
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
    staged.write_text(json.dumps(WEB_CREDS))
    staged.chmod(0o600)

    result = CliRunner().invoke(cli, ["--add", "foo/bar", "--db-path", db_path])

    assert result.exit_code != 0, "Expected non-zero exit for alias with slash"
    combined = result.output + (str(result.exception) if result.exception else "")
    assert (
        "invalid alias" in combined.lower() or
        "alias must match" in combined.lower()
    ), f"Expected alias validation error in output: {combined}"


# ---------------------------------------------------------------------------
# Test 8: --help includes Google Cloud Console setup (Web Application)
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
        "Web application",
        "https://mail.google.com/",
        "Authorized redirect URI",
        "Testing",
        "chronos --use",
        "chronos --add",
    ]
    for substr in required_substrings:
        assert substr in result.output, (
            f"Expected '{substr}' in --help output.\n"
            f"Full output:\n{result.output}"
        )
    # Desktop-app guidance must NOT survive the 05.2 migration
    assert "Desktop app" not in result.output, (
        "Help text must not mention 'Desktop app' after the 05.2 Web App migration."
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


# ---------------------------------------------------------------------------
# Test 10 (NEW): --use rejects Web JSON missing the configured redirect URI
# ---------------------------------------------------------------------------

def test_use_rejects_missing_redirect_uri(tmp_chronos_home, tmp_path):
    """If creds['web']['redirect_uris'] doesn't contain the configured callback
    URL, --use must exit non-zero with an Authorized-redirect-URI pointer."""
    creds_file = tmp_path / "web_creds_mismatch.json"
    creds_file.write_text(json.dumps({
        "web": {
            "client_id": "web-client.apps.googleusercontent.com",
            "client_secret": "web-secret",
            # Wrong port — does not match default 9004
            "redirect_uris": ["http://localhost:9999/callback"],
        }
    }))

    result = CliRunner().invoke(cli, ["--use", str(creds_file)])

    assert result.exit_code != 0, "Expected non-zero exit for missing redirect URI"
    combined = result.output + (str(result.exception) if result.exception else "")
    assert "Authorized redirect URI" in combined, (
        f"Expected 'Authorized redirect URI' in error: {combined}"
    )
    assert "http://localhost:9004/callback" in combined, (
        f"Expected configured callback URL in error: {combined}"
    )


# ---------------------------------------------------------------------------
# Test 11 (NEW): oauth.callback_port is configurable via config.yml
# ---------------------------------------------------------------------------

def test_oauth_callback_port_configurable(tmp_chronos_home, tmp_path, monkeypatch):
    """Writing oauth.callback_port=8765 into config.yml causes --use to accept
    Web creds whose redirect URI uses port 8765."""
    # Write a config.yml that overrides the callback port
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "settings:\n"
        "  oauth:\n"
        "    callback_port: 8765\n"
        "    callback_path: /callback\n"
    )
    monkeypatch.setenv("CHRONOS_CONFIG", str(cfg))

    # Force config to pick up the new env var on the next access
    from chronos.config import reset_cache
    reset_cache()

    # Web creds whose redirect URI matches the overridden port
    creds_file = tmp_path / "web_creds_8765.json"
    creds_file.write_text(json.dumps({
        "web": {
            "client_id": "web-client.apps.googleusercontent.com",
            "client_secret": "web-secret",
            "redirect_uris": ["http://localhost:8765/callback"],
        }
    }))

    result = CliRunner().invoke(cli, ["--use", str(creds_file)])

    assert result.exit_code == 0, (
        f"Expected exit 0 with custom callback_port, got {result.exit_code}\n"
        f"Output: {result.output}\nException: {result.exception}"
    )
    staged = tmp_chronos_home / "pending_credentials.json"
    assert staged.exists(), "Staged credentials file should exist"

    # Verify auth.py's redirect URI builder also honors the new port
    from chronos.cli.auth import _redirect_uri
    assert _redirect_uri() == "http://localhost:8765/callback", (
        f"auth._redirect_uri() should reflect overridden port, got {_redirect_uri()!r}"
    )


# ---------------------------------------------------------------------------
# Test 12 (NEW): token-load gate rejects legacy gmail.readonly+modify scopes
# ---------------------------------------------------------------------------

def test_token_load_rejects_legacy_scopes(tmp_chronos_home):
    """A token file written with the pre-05.2 scope set must be rejected with
    a clear migration message pointing the operator at --use + --add."""
    from pathlib import Path
    from chronos.cli.auth import _check_token_scopes

    legacy_scopes = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/calendar",
    ]
    token_file = tmp_chronos_home / "legacy_token.json"
    token_file.write_text(json.dumps({"scopes": legacy_scopes}))

    with pytest.raises(SystemExit) as excinfo:
        _check_token_scopes({"scopes": legacy_scopes}, token_file)

    msg = str(excinfo.value)
    assert "old scope set" in msg, f"Expected 'old scope set' in error: {msg}"
    assert "chronos --use" in msg, f"Expected '--use' migration pointer: {msg}"
    assert "chronos --add" in msg, f"Expected '--add' migration pointer: {msg}"


# ---------------------------------------------------------------------------
# Test 13 (NEW): tokens with the new mail.google.com scope pass the gate
# ---------------------------------------------------------------------------

def test_token_load_accepts_new_scopes(tmp_chronos_home):
    """A token written by the new flow (mail.google.com + calendar) must pass
    the scope gate without raising."""
    from pathlib import Path
    from chronos.cli.auth import _check_token_scopes

    new_scopes = [
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/calendar",
    ]
    token_file = tmp_chronos_home / "new_token.json"
    token_file.write_text(json.dumps({"scopes": new_scopes}))

    # Should not raise
    _check_token_scopes({"scopes": new_scopes}, token_file)
