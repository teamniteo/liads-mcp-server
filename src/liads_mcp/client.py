"""HTTP client for LinkedIn's versioned Marketing API (https://api.linkedin.com/rest)."""

from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

from .auth import TokenStore
from .config import Config

BASE_URL = "https://api.linkedin.com/rest"
MAX_RESPONSE_BYTES = 2_000_000
RETRY_STATUSES = {429, 500, 502, 503, 504}


class LinkedInError(RuntimeError):
    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(f"LinkedIn {status}: {message}" + (f" ({code})" if code else ""))
        self.status = status
        self.message = message
        self.code = code


def _error(resp: httpx.Response) -> LinkedInError:
    message, code = resp.text[:500], None
    try:
        body = resp.json()
        message = body.get("message") or message
        code = body.get("serviceErrorCode") or body.get("code")
        code = str(code) if code is not None else None
        details = _details(body.get("errorDetails"))
        if details:
            message = f"{message} Details: {details}"
    except (ValueError, AttributeError):
        pass
    return LinkedInError(resp.status_code, message, code)


def _details(error_details) -> str:
    """LinkedIn's per-field validation errors, e.g. 'optimizationTargetType: invalid value (INVALID_VALUE)'."""
    if not isinstance(error_details, dict):
        return ""
    parts = []
    for err in error_details.get("inputErrors") or []:
        path = ((err.get("input") or {}).get("inputPath") or {}).get("fieldPath", "")
        desc = err.get("description") or err.get("message") or ""
        code = err.get("code")
        parts.append(f"{path}: {desc}".strip(": ") + (f" ({code})" if code else ""))
    if not parts:
        import json

        return json.dumps(error_details)[:800]
    return "; ".join(parts)[:1500]


def _allowed_upload_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return urlparse(url).scheme == "https" and (host == "linkedin.com" or host.endswith(".linkedin.com"))


class Client:
    """Thin wrapper: auth and version headers, Rest.li header, retries on GET only."""

    def __init__(self, cfg: Config, tokens: TokenStore | None = None, http: httpx.Client | None = None):
        self.cfg = cfg
        self.tokens = tokens or TokenStore(cfg)
        self.http = http or httpx.Client(timeout=30)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.tokens.access_token()}",
            "LinkedIn-Version": self.cfg.linkedin.api_version,
            "X-Restli-Protocol-Version": "2.0.0",
        }
        headers.update(extra or {})
        return headers

    def request(self, method: str, path: str, query: str = "", json=None, headers: dict | None = None) -> httpx.Response:
        url = BASE_URL + path + (f"?{query}" if query else "")
        attempts = 3 if method == "GET" else 1  # never repeat a write: it could create duplicates
        for attempt in range(attempts):
            resp = self.http.request(method, url, json=json, headers=self._headers(headers))
            if resp.status_code == 401 and attempt == 0 and method == "GET":
                self.tokens.refresh()
                continue
            if resp.status_code in RETRY_STATUSES and attempt < attempts - 1:
                time.sleep(2**attempt)
                continue
            break
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise LinkedInError(resp.status_code, "response too large; narrow the request")
        if resp.status_code >= 400:
            raise _error(resp)
        return resp

    def get(self, path: str, query: str = "") -> dict:
        resp = self.request("GET", path, query)
        return resp.json() if resp.content else {}

    def create(self, path: str, body: dict, query: str = "") -> dict:
        """POST a new entity; returns the id from x-restli-id (or the body's id) and the body."""
        resp = self.request("POST", path, query, json=body)
        payload = resp.json() if resp.content else {}
        entity_id = resp.headers.get("x-restli-id") or resp.headers.get("x-linkedin-id") or payload.get("id")
        return {"id": entity_id, "body": payload}

    def partial_update(self, path: str, fields: dict) -> None:
        self.request("POST", path, json={"patch": {"$set": fields}}, headers={"X-RestLi-Method": "PARTIAL_UPDATE"})

    def put(self, path: str, body: dict) -> None:
        self.request("PUT", path, json=body)

    def batch_create(self, path: str, elements: list[dict]) -> dict:
        resp = self.request("POST", path, json={"elements": elements}, headers={"X-RestLi-Method": "BATCH_CREATE"})
        return resp.json() if resp.content else {}

    def upload_bytes(self, upload_url: str, data: bytes) -> None:
        if not _allowed_upload_host(upload_url):
            raise LinkedInError(0, f"refusing to upload to a non-LinkedIn host: {urlparse(upload_url).hostname}")
        resp = self.http.put(upload_url, content=data, headers={"Authorization": f"Bearer {self.tokens.access_token()}"})
        if resp.status_code >= 400:
            raise _error(resp)
