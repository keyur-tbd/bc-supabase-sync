#!/usr/bin/env python3
"""ONE-TIME: get a Gmail refresh token so the pipelines can email alerts.

You run this once, on your own machine, in a browser you are signed into as
birbal@thebakersdozen.in. It prints a refresh token. That token, plus the
client id and secret, go into GitHub secrets and never need touching again
unless someone revokes them.

    python scripts/gmail_authorize.py "C:/path/to/client_secret_....json" --set-secrets

--set-secrets writes the GitHub secrets straight from memory, so the refresh
token is never printed, never copied, and never lands in a terminal scrollback
or a chat transcript. Add repo names to do several at once:

    python scripts/gmail_authorize.py "...json" --set-secrets         keyur-tbd/bc-supabase-sync keyur-tbd/marketplace-ads-pipeline

Why not an app password: Google Workspace has them disabled here. Why not a
service account: that needs Workspace-admin domain-wide delegation. This uses
an OAuth client that already exists in the mail-grn project.

The only scope requested is gmail.send - permission to send, not to read.

The only scope requested is gmail.send - permission to send, not to read.
"""
from __future__ import annotations

import http.server
import json
import secrets
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

import requests

SCOPE = "https://www.googleapis.com/auth/gmail.send"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
SEP = "=" * 72
PORT = 8080                      # must match a redirect_uri on the OAuth client


class _Catch(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None

    def do_GET(self):                                    # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catch.code = (q.get("code") or [None])[0]
        _Catch.state = (q.get("state") or [None])[0]
        ok = _Catch.code is not None
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<h2>Done - you can close this tab and go back to the terminal.</h2>"
            if ok else
            b"<h2>No authorisation code came back. Check the terminal.</h2>")

    def log_message(self, *_):                           # keep the console clean
        pass


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    cfg = cfg[list(cfg.keys())[0]]                       # 'web' or 'installed'
    client_id, client_secret = cfg["client_id"], cfg["client_secret"]

    redirect = f"http://localhost:{PORT}/"
    if redirect.rstrip("/") not in [u.rstrip("/") for u in cfg.get("redirect_uris", [])]:
        print(f"ERROR: {redirect} is not a redirect URI on this OAuth client.")
        print("       Add it in Google Cloud Console > Credentials, or use a client that has it.")
        return 2

    state = secrets.token_urlsafe(16)                    # guards against a stray callback
    params = {
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "state": state,
        "access_type": "offline",   # <- without this there is NO refresh token
        "prompt": "consent",        # <- forces a refresh token even if previously granted
    }
    url = f"{AUTH}?{urllib.parse.urlencode(params)}"

    server = http.server.HTTPServer(("localhost", PORT), _Catch)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print("\nOpening your browser. Sign in as birbal@thebakersdozen.in and click Allow.")
    print("If nothing opens, paste this into a browser:\n")
    print(f"  {url}\n")
    webbrowser.open(url)

    for _ in range(300):                                 # up to ~5 minutes
        if _Catch.code:
            break
        threading.Event().wait(1)
    server.server_close()

    if not _Catch.code:
        print("Timed out waiting for authorisation.")
        return 3
    if _Catch.state != state:
        print("State mismatch - ignoring this callback.")
        return 4

    r = requests.post(TOKEN, timeout=60, data={
        "code": _Catch.code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code"})
    if r.status_code != 200:
        print("Token exchange failed:", r.status_code, r.text[:400])
        return 5
    tok = r.json()
    if "refresh_token" not in tok:
        print("Google did not return a refresh token. Revoke this app's access at")
        print("https://myaccount.google.com/permissions and run this again.")
        return 6

    refresh_token = tok["refresh_token"]
    sender = "birbal@thebakersdozen.in"
    values = {
        "GMAIL_CLIENT_ID": client_id,
        "GMAIL_CLIENT_SECRET": client_secret,
        "GMAIL_REFRESH_TOKEN": refresh_token,
        "GMAIL_SENDER": sender,
    }

    if "--set-secrets" in sys.argv:
        repos = [a for a in sys.argv[2:] if not a.startswith("--")]
        targets = repos or [None]          # None = whichever repo we are inside
        print(SEP)
        failed = False
        for repo in targets:
            label = repo or "(current repo)"
            for name, value in values.items():
                cmd = ["gh", "secret", "set", name, "--body", value]
                if repo:
                    cmd += ["--repo", repo]
                # capture output so a value can never be echoed back on failure
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode != 0:
                    failed = True
                    print("  FAILED  {}  {}: {}".format(label, name, r.stderr.strip()[:120]))
                else:
                    print("  set     {}  {}".format(label, name))
        print(SEP)
        print("Written straight from memory - the refresh token was never printed.")
        if failed:
            print("Some secrets failed. Fix the errors above and re-run; re-running is safe.")
            return 7
        return 0

    print(SEP)
    print("Success. The refresh token is a credential: prefer --set-secrets, which")
    print("writes it without ever showing it. Printing it only because you did not.")
    print("")
    print('  gh secret set GMAIL_CLIENT_ID     --body "{}"'.format(client_id))
    print('  gh secret set GMAIL_CLIENT_SECRET --body "<the client_secret from that json>"')
    print('  gh secret set GMAIL_REFRESH_TOKEN --body "{}"'.format(refresh_token))
    print('  gh secret set GMAIL_SENDER        --body "{}"'.format(sender))
    print(SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
