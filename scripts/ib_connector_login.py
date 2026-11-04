"""Log this repo in to IBKR's official connector, read-only. You sign in at IBKR, in your browser.

Registers a public client once, asks for ``mcp.read`` alone, and keeps the tokens encrypted under
``state/``. Revoke at any time: Client Portal > Settings > Manage Third-Party Consents.
"""
from __future__ import annotations

import http.server
import json
import secrets
import webbrowser
from urllib.parse import parse_qs, urlparse

import httpx

from trading_live_claude.brokers.ib_connector import (
    authorize_url,
    exchange_code,
    pkce_pair,
    register_client,
)
from trading_live_claude.brokers.token_store import TokenStore
from trading_live_claude.config import get_settings

PORT = 8765
REDIRECT = f"http://127.0.0.1:{PORT}/callback"


def wait_for_redirect() -> dict[str, list[str]]:
    got: dict[str, list[str]] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            got.update(parse_qs(urlparse(self.path).query))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"You can close this tab.")

        def log_message(self, *_: object) -> None:   # the query string carries the code
            pass

    with http.server.HTTPServer(("127.0.0.1", PORT), Handler) as server:
        while not got:
            server.handle_request()
    return got


def main() -> None:
    settings = get_settings()
    if not settings.token_encryption_key:
        raise SystemExit("TOKEN_ENCRYPTION_KEY must be set in .env.")
    state = settings.state_dir
    state.mkdir(parents=True, exist_ok=True)
    client_file = state / "ib_connector_client.json"
    with httpx.Client(timeout=30.0) as http_client:
        if client_file.exists():
            client_id = json.loads(client_file.read_text(encoding="utf-8"))["client_id"]
        else:
            client_id = register_client(http_client, REDIRECT)
            client_file.write_text(json.dumps({"client_id": client_id}), encoding="utf-8")
        verifier, challenge = pkce_pair()
        expected = secrets.token_urlsafe(24)
        url = authorize_url(client_id, REDIRECT, challenge, expected)
        print(f"Sign in at IBKR, choosing your PAPER account if asked:\n{url}", flush=True)
        webbrowser.open(url)
        reply = wait_for_redirect()
        if reply.get("state", [""])[0] != expected or "code" not in reply:
            raise SystemExit(f"Login failed: {reply.get('error', ['state mismatch'])[0]}")
        tokens = exchange_code(http_client, client_id, REDIRECT, reply["code"][0], verifier)
    TokenStore(state / "ib_connector_tokens.json.enc", settings.token_encryption_key).save(tokens)
    print("Logged in, read-only. Next: python scripts/paper_ib.py --transport connector --iterations 2")


if __name__ == "__main__":
    main()
