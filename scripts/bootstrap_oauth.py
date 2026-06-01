"""One-time local OAuth flow to capture a Graph refresh token.

Runs Microsoft identity platform v2 authorization-code + PKCE in your browser,
then prints the refresh token to stdout. Copy it into Secrets Manager:

    aws secretsmanager create-secret \\
        --name weekly-agenda-bot/graph \\
        --secret-string '{"client_id":"...","tenant_id":"common","refresh_token":"..."}'

Usage:
    export GRAPH_CLIENT_ID=...
    export GRAPH_TENANT_ID=common      # or 'consumers' for personal MSA
    python scripts/bootstrap_oauth.py
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import os
import secrets
import socketserver
import urllib.parse
import webbrowser

import httpx

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"
SCOPES = (
    "https://graph.microsoft.com/Mail.Read "
    "https://graph.microsoft.com/Calendars.ReadWrite "
    "https://graph.microsoft.com/Tasks.ReadWrite "
    "https://graph.microsoft.com/Files.ReadWrite "
    "https://graph.microsoft.com/User.Read "
    "offline_access"
)


class _CodeCatcher(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CodeCatcher.code = (params.get("code") or [None])[0]
        _CodeCatcher.error = (params.get("error_description") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        body = "<h1>Done — you can close this tab.</h1>" if _CodeCatcher.code else \
               f"<h1>Failed: {_CodeCatcher.error}</h1>"
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_a):  # silence default logging
        return


def main() -> None:
    client_id = os.environ["GRAPH_CLIENT_ID"]
    tenant = os.environ.get("GRAPH_TENANT_ID", "common")

    verifier = secrets.token_urlsafe(96)[:128]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)

    auth_url = (
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?"
        + urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        })
    )

    server = socketserver.TCPServer((REDIRECT_HOST, REDIRECT_PORT), _CodeCatcher)

    print(f"Opening browser to:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # Blocks until the redirect comes in and the handler sets code/error,
    # then returns. No busy-wait, no threading.
    server.handle_request()

    if _CodeCatcher.error:
        raise SystemExit(f"OAuth failed: {_CodeCatcher.error}")
    code = _CodeCatcher.code

    token_resp = httpx.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "scope": SCOPES,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    token_resp.raise_for_status()
    payload = token_resp.json()

    print("\n=== Refresh token ===")
    print(payload["refresh_token"])
    print()
    print("Store in Secrets Manager under weekly-agenda-bot/graph as:")
    print(f'  {{"client_id":"{client_id}","tenant_id":"{tenant}","refresh_token":"<above>"}}')


if __name__ == "__main__":
    main()
