"""Mint a NEW YouTube refresh token carrying both upload and analytics scopes.

A refresh token permanently carries whatever scopes were granted at the moment
it was issued. Adding a scope to the OAuth consent screen does NOT retrofit a
token that already exists — the token must be re-minted. That is why "I granted
analytics access" and "the API returns 403" are routinely both true.

This runs the OAuth loopback flow locally: it opens a consent page in your
browser, catches the redirect on 127.0.0.1, exchanges the code, and prints the
refresh token. Nothing is written to disk and nothing is sent anywhere except
Google.

The client id/secret are read from config.toml (`[app] youtube_client_id` /
`youtube_client_secret`), the same keys `youtube_upload.py` already falls back
to, with environment variables taking precedence. config.toml is gitignored, so
the credentials stay out of version control.

Usage:
    uv run python scripts/run_youtube_auth.py          # creds from config.toml
    YT_CLIENT_ID=... YT_CLIENT_SECRET=... uv run python scripts/run_youtube_auth.py

    # if your OAuth client is a "Web application", pin the port so you can
    # register exactly one redirect URI:
    YT_OAUTH_PORT=8765 uv run python scripts/run_youtube_auth.py
"""
import os
import socket
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# The project is not installed as a package (`[tool.uv] package = false`), so
# the repo root has to be on sys.path for `app.*` to import from a subdirectory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import config  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

_result = {}


def _cred(env_name: str, config_key: str) -> str:
    """Env var wins, config.toml [app] is the fallback — same order as youtube_upload."""
    return (os.environ.get(env_name) or config.app.get(config_key, "") or "").strip()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _result["code"] = (params.get("code") or [None])[0]
        _result["error"] = (params.get("error") or [None])[0]

        ok = _result["code"] is not None
        body = (
            "<h2>Authorisation received.</h2><p>You can close this tab and "
            "return to the terminal.</p>"
            if ok
            else f"<h2>Authorisation failed.</h2><p>{_result['error']}</p>"
        )
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):  # silence the default stderr access log
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    client_id = _cred("YT_CLIENT_ID", "youtube_client_id")
    client_secret = _cred("YT_CLIENT_SECRET", "youtube_client_secret")
    if not (client_id and client_secret):
        print("❌ No OAuth client credentials found.")
        print("   Add them under [app] in config.toml:")
        print('     youtube_client_id = "....apps.googleusercontent.com"')
        print('     youtube_client_secret = "GOCSPX-..."')
        print("   (or export YT_CLIENT_ID / YT_CLIENT_SECRET instead).")
        print("   These are the SAME values already in your GitHub secrets —")
        print("   only the refresh token needs to change.")
        return 1

    print(f"client id: {client_id[:12]}…{client_id[-24:]}")

    port = int(os.environ.get("YT_OAUTH_PORT") or _free_port())
    redirect_uri = f"http://127.0.0.1:{port}"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # offline -> issue a refresh token at all.
        "access_type": "offline",
        # consent -> force a NEW refresh token. Without this Google returns only
        # an access token for an already-consented app, and you would silently
        # end up re-using the old, under-scoped refresh token.
        "prompt": "consent",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    print(f"redirect URI in use:  {redirect_uri}")
    print(
        "\nIf your OAuth client is a *Web application*, this exact URI must be "
        "listed under 'Authorised redirect URIs' in the Google Cloud console.\n"
        "If it is a *Desktop app*, any loopback port is accepted already.\n"
    )
    print("Opening the consent screen. Approve BOTH requested permissions.\n")
    print(f"If no browser opens, paste this in manually:\n{url}\n")

    server = HTTPServer(("127.0.0.1", port), _Handler)
    webbrowser.open(url)
    server.handle_request()  # blocks until Google redirects back once
    server.server_close()

    if _result.get("error") or not _result.get("code"):
        print(f"❌ authorisation failed: {_result.get('error') or 'no code returned'}")
        return 1

    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": _result["code"],
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"❌ token exchange failed ({resp.status_code}): {resp.text[:500]}")
        return 1

    payload = resp.json()
    refresh_token = payload.get("refresh_token")
    granted = str(payload.get("scope", "")).split()

    print("\ngranted scopes:")
    for s in sorted(granted):
        print(f"   - {s}")

    missing = [s for s in SCOPES if s not in granted]
    if missing:
        print("\n❌ these scopes were NOT granted:")
        for s in missing:
            print(f"   - {s}")
        print("   Re-run and make sure every checkbox on the consent screen is ticked.")
        return 1

    if not refresh_token:
        print(
            "\n❌ Google returned no refresh_token. This happens when the app was "
            "already consented and 'prompt=consent' was dropped. Revoke access at "
            "https://myaccount.google.com/permissions and run this again."
        )
        return 1

    print("\n✅ Both scopes granted. Your new refresh token:\n")
    print(refresh_token)
    print(
        "\nNext: replace the YT_REFRESH_TOKEN secret with the value above.\n"
        "  gh secret set YT_REFRESH_TOKEN -R globalinsightist-lang/ytb\n"
        "\nTreat that string like a password — it grants upload access to the channel."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
