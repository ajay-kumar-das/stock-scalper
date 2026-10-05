"""Daily Upstox login helper — run on YOUR machine each trading morning (doc 03 §5).

    set UPSTOX_API_KEY, UPSTOX_API_SECRET, UPSTOX_REDIRECT_URI (e.g. http://127.0.0.1:8765/callback,
    which must match the redirect URI registered in your Upstox app), then:
    python scripts/upstox_login.py

Opens the Upstox login page, captures the redirect locally, exchanges the code for the day's
access token and stores it in ~/.scalper/token.json (file mode 600). The token is never printed in
full or logged (ADR-10).
"""
import json
import os
import stat
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AUTH = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN = "https://api.upstox.com/v2/login/authorization/token"


def main() -> None:
    key, secret, redirect = (os.environ.get(k) for k in ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI"))
    if not all((key, secret, redirect)):
        sys.exit("Set UPSTOX_API_KEY, UPSTOX_API_SECRET and UPSTOX_REDIRECT_URI first.")
    parsed = urllib.parse.urlparse(redirect)
    got: dict = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got["code"] = (q.get("code") or [""])[0]
            self.send_response(200); self.end_headers()
            self.wfile.write(b"Login received. You can close this tab.")

        def log_message(self, *a):
            pass

    srv = HTTPServer((parsed.hostname, parsed.port or 80), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    url = AUTH + "?" + urllib.parse.urlencode({"response_type": "code", "client_id": key, "redirect_uri": redirect})
    print("Opening Upstox login…")
    webbrowser.open(url)
    srv.timeout = 300
    while "code" not in got:
        srv.handle_request()
    data = urllib.parse.urlencode({"code": got["code"], "client_id": key, "client_secret": secret,
                                   "redirect_uri": redirect, "grant_type": "authorization_code"}).encode()
    req = urllib.request.Request(TOKEN, data=data, headers={"Accept": "application/json",
                                                             "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode())
    token = resp.get("access_token")
    if not token:
        sys.exit(f"Token exchange failed: { {k: v for k, v in resp.items() if k != 'access_token'} }")
    path = Path.home() / ".scalper" / "token.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"date": date.today().isoformat(), "access_token": token}))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    print(f"Token for {date.today()} saved to {path} (…{token[-4:]})")


if __name__ == "__main__":
    main()
