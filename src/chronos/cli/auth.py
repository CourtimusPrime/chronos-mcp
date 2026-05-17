"""OAuth2 PKCE authentication flow for Google accounts."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import httpx
from ulid import ULID

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/calendar",
]

# Legacy scopes that indicate a pre-05.2 token written with read+modify only.
_LEGACY_GMAIL_SCOPES = {
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
}
_NEW_GMAIL_SCOPE = "https://mail.google.com/"


def _oauth_callback() -> tuple[int, str]:
    """Return (callback_port, callback_path) from config."""
    from chronos.config import get as _cfg
    port = int(_cfg("oauth.callback_port", 9004))
    path = str(_cfg("oauth.callback_path", "/callback"))
    if not path.startswith("/"):
        path = "/" + path
    return port, path


def _redirect_uri() -> str:
    port, path = _oauth_callback()
    return f"http://localhost:{port}{path}"


def _check_token_scopes(token_data: dict, token_file: Path) -> None:
    """Raise SystemExit if token was written with the legacy Gmail scope pair.

    Legacy = missing mail.google.com AND contains gmail.readonly or gmail.modify.
    Forces the operator to re-authenticate against the new Web App OAuth client.
    """
    scopes = token_data.get("scopes") or []
    scopes_set = set(scopes)
    if _NEW_GMAIL_SCOPE in scopes_set:
        return
    if scopes_set & _LEGACY_GMAIL_SCOPES:
        alias = token_file.stem.removesuffix("_token")
        raise SystemExit(
            f"Token file ~/.chronos/{alias}_token.json was created with the "
            f"old scope set (gmail.readonly + gmail.modify). Re-authenticate "
            f"to grant the new scope: chronos --use <web_creds.json> && "
            f"chronos --add {alias}."
        )


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge."""
    code_verifier = secrets.token_urlsafe(96)[:128]
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def _get_chronos_home() -> Path:
    """Return ~/.chronos or $CHRONOS_HOME."""
    home = os.environ.get("CHRONOS_HOME", str(Path.home() / ".chronos"))
    return Path(home)


class _CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler to capture the OAuth2 callback."""

    auth_code: Optional[str] = None
    error: Optional[str] = None

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            body = b"<html><body><h2>Chronos: Authorization complete. You may close this window.</h2></body></html>"
        elif "error" in params:
            _CallbackHandler.error = params["error"][0]
            body = b"<html><body><h2>Chronos: Authorization failed. You may close this window.</h2></body></html>"
        else:
            body = b"<html><body><h2>Chronos: Unexpected callback.</h2></body></html>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        pass  # suppress default HTTP server logging


def _wait_for_callback(
    callback_port: int, timeout: int = 300
) -> tuple[Optional[str], Optional[str]]:
    """Start local HTTP server and wait for OAuth2 callback."""
    _CallbackHandler.auth_code = None
    _CallbackHandler.error = None

    server = HTTPServer(("localhost", callback_port), _CallbackHandler)
    server.timeout = 1.0

    deadline = time.time() + timeout
    while time.time() < deadline:
        server.handle_request()
        if _CallbackHandler.auth_code or _CallbackHandler.error:
            break

    server.server_close()
    return _CallbackHandler.auth_code, _CallbackHandler.error


def run_oauth_flow(
    alias: str,
    credentials_path: str,
    conn: sqlite3.Connection,
) -> None:
    """
    Execute the full OAuth2 PKCE flow for Google (Web Application client).

    Steps (from PRD §10.1, updated for Phase 05.2):
    1. Read credentials JSON (expect 'web' key — Web Application OAuth client)
    2. Generate PKCE code_verifier/challenge
    3. Open browser to consent URL (redirect_uri from oauth.callback_port config)
    4. Listen on localhost:<callback_port> for callback
    5. Exchange code for tokens
    6. Error if no refresh_token
    7. Write self-contained token file
    8. Test Gmail + Calendar APIs
    9. Write two accounts rows
    10. Print confirmation summary
    """
    from rich.console import Console
    console = Console()

    callback_port, callback_path = _oauth_callback()
    redirect_uri = f"http://localhost:{callback_port}{callback_path}"

    # Step 1: Read credentials JSON
    creds_path = Path(credentials_path)
    if not creds_path.exists():
        raise SystemExit(f"Error: file not found: {credentials_path}")

    try:
        creds_data = json.loads(creds_path.read_text())
    except json.JSONDecodeError:
        raise SystemExit("Error: could not parse credentials file")

    if "installed" in creds_data:
        raise SystemExit(
            "Desktop App credentials are no longer supported. Create a Web "
            "Application OAuth client in Google Cloud Console (Application "
            f"type: Web application), add {redirect_uri} as an Authorized "
            "redirect URI, then re-download the JSON. Run `chronos --help` "
            "for full setup instructions."
        )

    if "web" not in creds_data:
        raise SystemExit("Error: could not parse credentials file")

    web = creds_data["web"]
    client_id = web["client_id"]
    client_secret = web["client_secret"]
    token_uri = web.get("token_uri", GOOGLE_TOKEN_URI)

    # Check alias not already registered
    existing = conn.execute(
        "SELECT id FROM accounts WHERE display_name = ? LIMIT 1", (alias,)
    ).fetchone()
    if existing:
        raise SystemExit(
            f'Error: alias "{alias}" already registered. Use --remove {alias} first, or choose a different alias.'
        )

    # Step 2: Generate PKCE
    code_verifier, code_challenge = _generate_pkce()

    # Step 3: Build consent URL and open browser
    state = secrets.token_urlsafe(16)
    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = GOOGLE_AUTH_URI + "?" + urllib.parse.urlencode(auth_params)

    console.print(f"\n[bold cyan]Opening browser for Google OAuth2 consent...[/bold cyan]")
    console.print(f"If the browser does not open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # Step 4: Wait for callback
    console.print(f"[dim]Waiting for OAuth2 callback on localhost:{callback_port}...[/dim]")
    auth_code, error = _wait_for_callback(callback_port)

    if error:
        raise SystemExit("Error: Google OAuth consent was declined")

    if not auth_code:
        raise SystemExit("Error: Google OAuth consent was declined")

    # Step 5: Exchange code for tokens
    token_data = {
        "code": auth_code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }

    with httpx.Client() as client:
        response = client.post(token_uri, data=token_data)

    if response.status_code != 200:
        raise SystemExit(f"Error: token exchange failed: {response.text}")

    token_response = response.json()

    # Step 6: Error if no refresh_token
    if "refresh_token" not in token_response:
        raise SystemExit(
            "Error: Google did not return a refresh token. "
            "Ensure prompt=consent is set and the OAuth flow completed fully."
        )

    access_token = token_response["access_token"]
    refresh_token = token_response["refresh_token"]
    expires_in = token_response.get("expires_in", 3600)
    token_expiry_unix = int((time.time() + expires_in) * 1000)

    # Step 7: Write self-contained token file
    chronos_home = _get_chronos_home()
    chronos_home.mkdir(parents=True, exist_ok=True)
    token_file = chronos_home / f"{alias}_token.json"

    # We need to get the email from the Gmail profile API
    headers = {"Authorization": f"Bearer {access_token}"}

    with httpx.Client() as client:
        # Step 8: Test Gmail profile API
        try:
            gmail_resp = client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                headers=headers,
            )
            gmail_resp.raise_for_status()
            gmail_profile = gmail_resp.json()
        except Exception as e:
            raise SystemExit(
                "Error: Gmail API test failed — check that Gmail API is enabled in your Google Cloud project"
            )

        email = gmail_profile.get("emailAddress", "unknown@gmail.com")
        messages_total = gmail_profile.get("messagesTotal", 0)

        # Test Calendar list API
        try:
            cal_resp = client.get(
                "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                headers=headers,
            )
            cal_resp.raise_for_status()
            cal_data = cal_resp.json()
        except Exception as e:
            raise SystemExit(
                "Error: Calendar API test failed — check that Google Calendar API is enabled in your Google Cloud project"
            )

        calendar_count = len(cal_data.get("items", []))

    # Write the token file (self-contained)
    token_file_data = {
        "email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expiry_unix": token_expiry_unix,
        "client_id": client_id,
        "client_secret": client_secret,
        "token_uri": token_uri,
        "scopes": SCOPES,
    }
    token_file.write_text(json.dumps(token_file_data, indent=2))
    token_file.chmod(0o600)

    # Step 9: Write two accounts rows
    now_ms = int(time.time() * 1000)
    auth_data = json.dumps({"token_file": str(token_file)})

    gmail_id = str(ULID())
    gcal_id = str(ULID())

    conn.execute(
        """
        INSERT INTO accounts (id, provider, email, display_name, auth_type, auth_data, sync_enabled, created_at)
        VALUES (?, 'gmail', ?, ?, 'oauth2', ?, 1, ?)
        """,
        (gmail_id, email, alias, auth_data, now_ms),
    )
    conn.execute(
        """
        INSERT INTO accounts (id, provider, email, display_name, auth_type, auth_data, sync_enabled, created_at)
        VALUES (?, 'google_calendar', ?, ?, 'oauth2', ?, 1, ?)
        """,
        (gcal_id, email, alias, auth_data, now_ms),
    )
    conn.commit()

    # Step 10: Print confirmation summary
    console.print(f"\n[bold green]✓ Account registered: {alias}[/bold green]")
    console.print(f"  Gmail:    {email}  ({messages_total:,} messages)")
    console.print(f"  Calendar: {calendar_count} calendars found")
    console.print(f"  Tokens:   {token_file}")
    console.print(f"\nRun [bold]chronos --start[/bold] to begin syncing.")


def test_account(alias: str, conn: sqlite3.Connection) -> None:
    """Re-run endpoint tests for an existing alias."""
    from rich.console import Console
    console = Console()

    rows = conn.execute(
        "SELECT * FROM accounts WHERE display_name = ?", (alias,)
    ).fetchall()

    if not rows:
        raise SystemExit(f"Error: alias '{alias}' not found. Use 'chronos --list' to see registered accounts.")

    # Find a token file from any of the rows
    token_file = None
    for row in rows:
        auth_data = json.loads(row["auth_data"])
        tf = Path(auth_data.get("token_file", ""))
        if tf.exists():
            token_file = tf
            break

    if not token_file:
        raise SystemExit(f"Error: token file not found for alias '{alias}'")

    token_data = json.loads(token_file.read_text())
    _check_token_scopes(token_data, token_file)
    access_token = _ensure_valid_token(token_data, token_file)

    headers = {"Authorization": f"Bearer {access_token}"}

    with httpx.Client() as client:
        try:
            gmail_resp = client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                headers=headers,
            )
            gmail_resp.raise_for_status()
            gmail_profile = gmail_resp.json()
        except Exception:
            raise SystemExit(
                "Error: Gmail API test failed — check that Gmail API is enabled in your Google Cloud project"
            )

        email = gmail_profile.get("emailAddress", "unknown")
        messages_total = gmail_profile.get("messagesTotal", 0)

        try:
            cal_resp = client.get(
                "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                headers=headers,
            )
            cal_resp.raise_for_status()
            cal_data = cal_resp.json()
        except Exception:
            raise SystemExit(
                "Error: Calendar API test failed — check that Google Calendar API is enabled in your Google Cloud project"
            )

        calendar_count = len(cal_data.get("items", []))

    console.print(f"\n[bold green]✓ Account tested: {alias}[/bold green]")
    console.print(f"  Gmail:    {email}  ({messages_total:,} messages)")
    console.print(f"  Calendar: {calendar_count} calendars found")
    console.print(f"  Tokens:   {token_file}")


def _ensure_valid_token(token_data: dict, token_file: Path) -> str:
    """Return a valid access token, refreshing if needed.

    Also gates legacy-scope tokens written before the 05.2 migration:
    tokens missing https://mail.google.com/ but containing the old
    gmail.readonly/gmail.modify pair are rejected with a migration hint.
    """
    _check_token_scopes(token_data, token_file)

    expiry = token_data.get("token_expiry_unix", 0)
    now_ms = int(time.time() * 1000)

    # Refresh if expired or expiring within 5 minutes
    if expiry - now_ms < 300_000:
        token_data = _refresh_token(token_data, token_file)

    return token_data["access_token"]


def _refresh_token(token_data: dict, token_file: Path) -> dict:
    """Refresh the access token and write back to the token file."""
    refresh_data = {
        "client_id": token_data["client_id"],
        "client_secret": token_data["client_secret"],
        "refresh_token": token_data["refresh_token"],
        "grant_type": "refresh_token",
    }

    with httpx.Client() as client:
        response = client.post(
            token_data.get("token_uri", GOOGLE_TOKEN_URI),
            data=refresh_data,
        )

    if response.status_code != 200:
        raise RuntimeError(f"Token refresh failed: {response.text}")

    resp_json = response.json()
    token_data["access_token"] = resp_json["access_token"]
    expires_in = resp_json.get("expires_in", 3600)
    token_data["token_expiry_unix"] = int((time.time() + expires_in) * 1000)

    # refresh_token may or may not be returned; keep existing if not
    if "refresh_token" in resp_json:
        token_data["refresh_token"] = resp_json["refresh_token"]

    token_file.write_text(json.dumps(token_data, indent=2))
    return token_data
