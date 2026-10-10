"""One-time Oura OAuth2 authorization (authorization-code flow).

Reads client credentials from .env, prints a consent URL, catches the redirect
on localhost, exchanges the code for tokens, and writes tokens.json.

Standard library only. Run:  python oura_auth.py
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
TOKEN_PATH = ROOT / "tokens.json"
API_TEST_URL = "https://api.ouraring.com/v2/usercollection/personal_info"
USER_AGENT = "home-health-dashboard/0.1"
CALLBACK_TIMEOUT_SECONDS = 600

REQUIRED_KEYS = (
    "OURA_CLIENT_ID",
    "OURA_CLIENT_SECRET",
    "OURA_REDIRECT_URI",
    "OURA_AUTHORIZE_URL",
    "OURA_TOKEN_URL",
)


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        sys.exit(f"Missing {path.name}. Copy .env.example to .env and fill it in.")
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    missing = [k for k in REQUIRED_KEYS if not env.get(k)]
    if missing:
        sys.exit(f"Missing values in .env: {', '.join(missing)}")
    return env


def build_authorize_url(env: dict[str, str], state: str) -> str:
    """Start from the portal's example URL so scope names match exactly."""
    parts = urllib.parse.urlsplit(env["OURA_AUTHORIZE_URL"])
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.update(
        {
            "client_id": env["OURA_CLIENT_ID"],
            "redirect_uri": env["OURA_REDIRECT_URI"],
            "response_type": "code",
            "state": state,
        }
    )
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), "")
    )


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != self.server.callback_path:  # type: ignore[attr-defined]
            self.send_response(404)
            self.end_headers()
            return
        params = dict(urllib.parse.parse_qsl(parts.query))
        self.server.result = params  # type: ignore[attr-defined]
        ok = "code" in params and "error" not in params
        body = (
            "Authorization received. You can close this tab."
            if ok
            else f"Authorization failed: {params.get('error', 'no code returned')}"
        )
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_args) -> None:
        # Default logging prints the request line, which contains the auth code.
        return


def wait_for_callback(redirect_uri: str) -> dict[str, str]:
    parts = urllib.parse.urlsplit(redirect_uri)
    host = parts.hostname or "localhost"
    port = parts.port or 80
    try:
        server = http.server.HTTPServer((host, port), CallbackHandler)
    except OSError as exc:
        sys.exit(f"Could not listen on {host}:{port} ({exc}). Is something else using it?")
    server.callback_path = parts.path or "/"  # type: ignore[attr-defined]
    server.result = None  # type: ignore[attr-defined]
    server.timeout = 1
    deadline = time.monotonic() + CALLBACK_TIMEOUT_SECONDS
    try:
        while server.result is None:  # type: ignore[attr-defined]
            if time.monotonic() > deadline:
                sys.exit("Timed out waiting for the Oura redirect. Run the script again.")
            server.handle_request()
    finally:
        server.server_close()
    return server.result  # type: ignore[attr-defined]


def exchange_code(env: dict[str, str], code: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": env["OURA_REDIRECT_URI"],
            "client_id": env["OURA_CLIENT_ID"],
            "client_secret": env["OURA_CLIENT_SECRET"],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        env["OURA_TOKEN_URL"],
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        host = urllib.parse.urlsplit(env["OURA_TOKEN_URL"]).netloc
        sys.exit(
            f"Token exchange failed at {host}: HTTP {exc.code}\n{detail}\n\n"
            "The authorization code is single-use, so it is now spent. If this was a "
            "401/404, switch OURA_TOKEN_URL in .env to the other endpoint listed in "
            ".env.example and run the script again."
        )
    except urllib.error.URLError as exc:
        sys.exit(f"Could not reach the token endpoint: {exc.reason}")


def save_tokens(tokens: dict, token_url: str) -> None:
    """Atomic write: a crash mid-write must not destroy the only refresh token."""
    record = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "token_type": tokens.get("token_type", "bearer"),
        "expires_in": tokens.get("expires_in"),
        "obtained_at": int(time.time()),
        "token_url": token_url,
    }
    fd, tmp_name = tempfile.mkstemp(dir=ROOT, prefix="tokens.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, TOKEN_PATH)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def smoke_test(access_token: str) -> str:
    request = urllib.request.Request(
        API_TEST_URL,
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} ({exc.read().decode('utf-8', errors='replace')[:200]})"
    except urllib.error.URLError as exc:
        return f"unreachable ({exc.reason})"


def main() -> None:
    env = load_env(ENV_PATH)
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(env, state)

    print("\nOpen this URL in a PRIVATE/INCOGNITO window, and have the ring owner")
    print("sign in with HER Oura account and approve:\n")
    print(url)
    print(f"\nWaiting for the redirect on {env['OURA_REDIRECT_URI']} ...")

    result = wait_for_callback(env["OURA_REDIRECT_URI"])
    if "error" in result:
        sys.exit(f"Oura returned an error: {result['error']}")
    if result.get("state") != state:
        sys.exit("State mismatch on the redirect. Not continuing. Run the script again.")
    if "code" not in result:
        sys.exit("No authorization code in the redirect. Run the script again.")

    tokens = exchange_code(env, result["code"])
    if "access_token" not in tokens:
        sys.exit(f"Token response had no access_token. Keys returned: {sorted(tokens)}")
    save_tokens(tokens, env["OURA_TOKEN_URL"])

    print(f"\nSaved {TOKEN_PATH.name}.")
    print(f"Refresh token present: {bool(tokens.get('refresh_token'))}")
    print(f"Access token lifetime: {tokens.get('expires_in')} seconds")
    print(f"API smoke test (personal_info): {smoke_test(tokens['access_token'])}")


if __name__ == "__main__":
    main()
