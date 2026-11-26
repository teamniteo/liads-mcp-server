"""OAuth 2.0 (3-legged) for LinkedIn, and the local token file.

`linkedin-ads-mcp login` opens LinkedIn's consent page, receives the code on
http://localhost:<redirect_port>/callback, checks the state value and stores
the tokens in a 0600 file. The server refreshes the access token with the
refresh token when it is about to expire.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import Config

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
REFRESH_MARGIN = 300  # seconds


class AuthError(RuntimeError):
    pass


def _client_credentials(cfg: Config) -> tuple[str, str]:
    data = json.loads(Path(cfg.linkedin.client_path).read_text(encoding="utf-8"))
    try:
        return data["client_id"], data["client_secret"]
    except KeyError as exc:
        raise AuthError(f"{cfg.linkedin.client_path} needs client_id and client_secret") from exc


def redirect_uri(cfg: Config) -> str:
    return f"http://localhost:{cfg.linkedin.redirect_port}/callback"


def _save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _token_record(resp: dict, previous: dict | None = None) -> dict:
    now = int(time.time())
    record = {
        "access_token": resp["access_token"],
        "expires_at": now + int(resp.get("expires_in", 0)),
        "scope": resp.get("scope", (previous or {}).get("scope", "")),
    }
    if resp.get("refresh_token"):
        record["refresh_token"] = resp["refresh_token"]
        record["refresh_token_expires_at"] = now + int(resp.get("refresh_token_expires_in", 0))
    elif previous and previous.get("refresh_token"):
        record["refresh_token"] = previous["refresh_token"]
        record["refresh_token_expires_at"] = previous.get("refresh_token_expires_at", 0)
    return record


def _exchange(data: dict) -> dict:
    resp = httpx.post(TOKEN_URL, data=data, timeout=30)
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error_description") or resp.json().get("error")
        except ValueError:
            msg = resp.text[:200]
        raise AuthError(f"LinkedIn token endpoint returned {resp.status_code}: {msg}")
    return resp.json()


class TokenStore:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = Path(cfg.linkedin.token_path)

    def load(self) -> dict:
        if not self.path.exists():
            raise AuthError("No token yet: run `linkedin-ads-mcp login`")
        return json.loads(self.path.read_text(encoding="utf-8"))

    def access_token(self) -> str:
        record = self.load()
        if record.get("expires_at", 0) - REFRESH_MARGIN > time.time():
            return record["access_token"]
        return self.refresh(record)["access_token"]

    def refresh(self, record: dict | None = None) -> dict:
        record = record or self.load()
        if not record.get("refresh_token"):
            raise AuthError("Access token expired and no refresh token: run `linkedin-ads-mcp login`")
        if record.get("refresh_token_expires_at") and record["refresh_token_expires_at"] < time.time():
            raise AuthError("Refresh token expired: run `linkedin-ads-mcp login`")
        client_id, client_secret = _client_credentials(self.cfg)
        resp = _exchange(
            {
                "grant_type": "refresh_token",
                "refresh_token": record["refresh_token"],
                "client_id": client_id,
                "client_secret": client_secret,
            }
        )
        new = _token_record(resp, record)
        _save(self.path, new)
        return new

    def status(self) -> dict:
        try:
            record = self.load()
        except AuthError as exc:
            return {"state": "missing", "detail": str(exc)}
        now = time.time()
        return {
            "state": "ok" if record.get("expires_at", 0) > now else "expired",
            "access_token_days_left": round((record.get("expires_at", 0) - now) / 86400, 1),
            "refresh_token": bool(record.get("refresh_token")),
            "refresh_token_days_left": round((record.get("refresh_token_expires_at", 0) - now) / 86400, 1)
            if record.get("refresh_token_expires_at")
            else None,
            "scopes": record.get("scope", ""),
        }


def login(cfg: Config, open_browser: bool = True) -> dict:
    client_id, client_secret = _client_credentials(cfg)
    state = secrets.token_urlsafe(24)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = parse_qs(parsed.query)
            result.update({k: v[0] for k, v in qs.items()})
            ok = result.get("state") == state and "code" in result
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab." if ok else b"Sign-in failed. Check the terminal.")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", cfg.linkedin.redirect_port), Handler)
    url = AUTHORIZE_URL + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri(cfg),
            "state": state,
            "scope": " ".join(cfg.linkedin.scopes),
        }
    )
    print("Open this URL to sign in to LinkedIn:\n" + url, flush=True)
    if open_browser:
        webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()
    if "error" in result:
        raise AuthError(f"LinkedIn refused: {result.get('error')}: {result.get('error_description', '')}")
    if result.get("state") != state:
        raise AuthError("State mismatch: sign-in aborted")
    resp = _exchange(
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri(cfg),
            "client_id": client_id,
            "client_secret": client_secret,
        }
    )
    record = _token_record(resp)
    _save(Path(cfg.linkedin.token_path), record)
    return {k: v for k, v in TokenStore(cfg).status().items()}
