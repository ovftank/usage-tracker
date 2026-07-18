import asyncio
import base64
import binascii
import hashlib
import html
import json
import os
import re
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


GO_AUTH_URL = "https://opencode.ai/auth"
GO_WORKSPACE_URL = "https://opencode.ai/workspace/{wid}/go"
GO_KEYS_URL = "https://opencode.ai/workspace/{wid}/keys"
USER_AGENT = "usage-tracker/0.1"
OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_ISSUER = "https://auth.openai.com"
OPENAI_CALLBACK_HOST = "127.0.0.1"
OPENAI_CALLBACK_PORT = 1455
OPENAI_CALLBACK_PATH = "/auth/callback"
OPENAI_CALLBACK_URL = f"http://localhost:{OPENAI_CALLBACK_PORT}{OPENAI_CALLBACK_PATH}"
OPENAI_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DATA_DIR = (
    Path(os.getenv("LOCALAPPDATA", Path(__file__).resolve().parent)) / "UsageTracker"
)
CACHE_FILE = DATA_DIR / "wid_cache.json"


@dataclass
class UsageItem:
    percent: int
    reset_in_sec: int


@dataclass
class AccountUsage:
    email: str | None
    rolling: UsageItem | None
    weekly: UsageItem | None
    monthly: UsageItem | None


@dataclass
class AccountResult:
    email: str
    usage: AccountUsage | None
    api_keys: list[str]
    error: str | None = None


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    with CACHE_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def _save_cache(cache: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CACHE_FILE.open("w", encoding="utf-8") as file:
        json.dump(cache, file, indent=2)


def _cache_key(cookie: str) -> str:
    return f"cache_{hashlib.sha256(cookie.encode()).hexdigest()}"


def _headers(cookie: str) -> dict:
    return {"User-Agent": USER_AGENT, "Cookie": cookie}


def _base64_url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _create_pkce() -> tuple[str, str]:
    verifier = _base64_url(secrets.token_bytes(32))
    challenge = _base64_url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _decode_jwt_claims(token: str) -> dict[str, object]:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        )
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return cast(dict[str, object], claims) if isinstance(claims, dict) else {}


def _extract_openai_account_id(tokens: dict[str, object]) -> str | None:
    for token_name in ("id_token", "access_token"):
        token = tokens.get(token_name)
        if not isinstance(token, str):
            continue
        claims = _decode_jwt_claims(token)
        account_id = claims.get("chatgpt_account_id")
        nested_auth = claims.get("https://api.openai.com/auth")
        organizations = claims.get("organizations")
        if isinstance(account_id, str) and account_id:
            return account_id
        nested_claims = (
            cast(dict[str, object], nested_auth)
            if isinstance(nested_auth, dict)
            else {}
        )
        nested_account_id = nested_claims.get("chatgpt_account_id")
        if isinstance(nested_account_id, str):
            return nested_account_id
        if isinstance(organizations, list) and organizations:
            first = organizations[0]
            organization = (
                cast(dict[str, object], first) if isinstance(first, dict) else {}
            )
            organization_id = organization.get("id")
            if isinstance(organization_id, str):
                return organization_id
    return None


def _openai_callback_page(title: str, message: str, error: bool = False) -> bytes:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
</head>
<body style="margin:0;background:#f0efef;color:#131010;font-family:Segoe UI,Arial,sans-serif;display:grid;min-height:100vh;place-items:center">
  <main style="width:min(400px,calc(100vw - 48px));box-sizing:border-box;background:#ffffff;border:1px solid #d4d2d2;border-top:4px solid {"#e81123" if error else "#5a5858"};padding:32px;text-align:center">
    <p style="margin:0;font-size:16px;font-weight:600">{html.escape(title)}</p>
    <p style="margin:12px 0 0;color:#5a5858;font-size:14px">{html.escape(message)}</p>
  </main>
</body>
</html>""".encode("utf-8")


def _openai_callback(state: str) -> str:
    callback: dict[str, str | None] = {"code": None, "error": None}
    completed = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def respond(
            self, status: int, title: str, message: str, error: bool = False
        ) -> None:
            body = _openai_callback_page(title, message, error)
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if parsed.path != OPENAI_CALLBACK_PATH:
                self.send_error(404, "Not found")
                return

            error = params.get("error_description", params.get("error", [None]))[0]
            code = params.get("code", [None])[0]
            callback_state = params.get("state", [None])[0]
            if error:
                callback["error"] = f"OpenAI authorization failed: {error}"
                self.respond(400, "Authorization did not stick", str(error), error=True)
            elif not code or callback_state != state:
                callback["error"] = "OpenAI callback was invalid"
                self.respond(
                    400,
                    "That callback looked suspicious",
                    "The sign-in response did not match this login attempt. Return to the app and try again.",
                    error=True,
                )
            else:
                callback["code"] = code
                self.respond(
                    200,
                    "ChatGPT connected",
                    "You can close this tab and return to Usage Tracker.",
                )
            completed.set()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer((OPENAI_CALLBACK_HOST, OPENAI_CALLBACK_PORT), CallbackHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    if not completed.wait(timeout=300):
        server.server_close()
        raise RuntimeError("OpenAI login timed out after 5 minutes")
    server.server_close()

    if callback["error"]:
        raise RuntimeError(str(callback["error"]))
    if not isinstance(callback["code"], str):
        raise RuntimeError("OpenAI callback did not return an authorization code")
    return callback["code"]


def authorize_openai() -> dict:
    verifier, challenge = _create_pkce()
    state = _base64_url(secrets.token_bytes(32))
    authorize_url = f"{OPENAI_ISSUER}/oauth/authorize?" + urlencode(
        {
            "response_type": "code",
            "client_id": OPENAI_CLIENT_ID,
            "redirect_uri": OPENAI_CALLBACK_URL,
            "scope": "openid profile email offline_access",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": "usage-tracker",
        }
    )
    if not webbrowser.open(authorize_url):
        raise RuntimeError("Could not open the OpenAI login browser")

    code = _openai_callback(state)
    with httpx.Client(timeout=30) as client:
        response = client.post(
            f"{OPENAI_ISSUER}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OPENAI_CALLBACK_URL,
                "client_id": OPENAI_CLIENT_ID,
                "code_verifier": verifier,
            },
        )
        response.raise_for_status()
        tokens = response.json()

    account_id = _extract_openai_account_id(tokens)
    if not account_id:
        raise RuntimeError("OpenAI token did not contain a ChatGPT account ID")
    return {
        "type": "oauth",
        "access": tokens["access_token"],
        "refresh": tokens["refresh_token"],
        "expires": int(datetime.now(timezone.utc).timestamp() * 1000)
        + int(tokens.get("expires_in", 3600)) * 1000,
        "accountId": account_id,
    }


def _openai_headers(auth: dict) -> dict:
    return {
        "Authorization": f"Bearer {auth['access']}",
        "ChatGPT-Account-Id": auth["accountId"],
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


async def _refresh_openai_auth(client: httpx.AsyncClient, auth: dict) -> None:
    response = await client.post(
        f"{OPENAI_ISSUER}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": auth["refresh"],
            "client_id": OPENAI_CLIENT_ID,
        },
    )
    response.raise_for_status()
    tokens = response.json()
    auth.update(
        {
            "access": tokens["access_token"],
            "refresh": tokens["refresh_token"],
            "expires": int(datetime.now(timezone.utc).timestamp() * 1000)
            + int(tokens.get("expires_in", 3600)) * 1000,
            "accountId": _extract_openai_account_id(tokens) or auth["accountId"],
        }
    )


def _openai_usage_item(window: object) -> UsageItem | None:
    if not isinstance(window, dict):
        return None
    data = cast(dict[str, object], window)
    used_percent = data.get("used_percent")
    reset_after_seconds = data.get("reset_after_seconds")
    if not isinstance(used_percent, (int, float)) or not isinstance(
        reset_after_seconds, (int, float)
    ):
        return None
    return UsageItem(
        percent=round(used_percent), reset_in_sec=round(reset_after_seconds)
    )


def _parse_usage_item(label: str, html: str) -> UsageItem | None:
    match = re.search(
        rf'{label}:\$R\[\d+\]=\{{status:"[^"]+",resetInSec:(\d+),usagePercent:(\d+)\}}',
        html,
    )
    if not match:
        return None
    return UsageItem(reset_in_sec=int(match[1]), percent=int(match[2]))


def _parse_usage(html: str) -> AccountUsage | None:
    rolling = _parse_usage_item("rollingUsage", html)
    weekly = _parse_usage_item("weeklyUsage", html)
    monthly = _parse_usage_item("monthlyUsage", html)
    if not (rolling and weekly and monthly):
        return None

    email_prefix = re.search(r'\$R\[\d+\],"', html)
    email = None
    if email_prefix:
        email_end = html.find('"', email_prefix.end())
        candidate = html[email_prefix.end() : email_end]
        if email_end != -1 and "@" in candidate:
            email = candidate
    return AccountUsage(
        email=email,
        rolling=rolling,
        weekly=weekly,
        monthly=monthly,
    )


async def resolve_workspace_id(client: httpx.AsyncClient, cookie: str) -> str | None:
    cache_key = _cache_key(cookie)
    cache = _load_cache()
    if cache_key in cache:
        return cache[cache_key]

    response = await client.get(
        GO_AUTH_URL,
        headers=_headers(cookie),
        follow_redirects=False,
    )
    workspace_match = re.search(
        r"workspace/(wrk_[^/?]+)",
        response.headers.get("location", ""),
    )
    if response.status_code != 302 or not workspace_match:
        return None

    workspace_id = workspace_match[1]
    cache[cache_key] = workspace_id
    _save_cache(cache)
    return workspace_id


async def _fetch_usage(
    client: httpx.AsyncClient,
    cookie: str,
    workspace_id: str,
) -> AccountUsage | None:
    response = await client.get(
        GO_WORKSPACE_URL.format(wid=workspace_id),
        headers=_headers(cookie),
    )
    return _parse_usage(response.text) if response.status_code == 200 else None


async def _fetch_api_keys(
    client: httpx.AsyncClient,
    cookie: str,
    workspace_id: str,
) -> list[str]:
    response = await client.get(
        GO_KEYS_URL.format(wid=workspace_id),
        headers=_headers(cookie),
    )
    if response.status_code != 200:
        return []
    return re.findall(r'key:"(sk-[^"]+)"', response.text)


async def fetch_account(client: httpx.AsyncClient, cookie: str) -> AccountResult:
    if not cookie:
        return AccountResult(email="?", usage=None, api_keys=[], error="No cookie")

    workspace_id = await resolve_workspace_id(client, cookie)
    if not workspace_id:
        return AccountResult(email="?", usage=None, api_keys=[], error="No workspace")

    usage, api_keys = await asyncio.gather(
        _fetch_usage(client, cookie, workspace_id),
        _fetch_api_keys(client, cookie, workspace_id),
    )
    return AccountResult(
        email=(usage.email if usage and usage.email else "?"),
        usage=usage,
        api_keys=api_keys,
        error=None if usage else "Usage data unavailable",
    )


async def fetch_openai_account(client: httpx.AsyncClient, auth: dict) -> AccountResult:
    required = ("access", "refresh", "expires", "accountId")
    if not all(auth.get(key) for key in required):
        return AccountResult(
            email="?", usage=None, api_keys=[], error="OpenAI OAuth data is incomplete"
        )

    try:
        expires = int(auth["expires"])
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        if expires <= now + 5 * 60 * 1000:
            await _refresh_openai_auth(client, auth)

        response = await client.get(OPENAI_USAGE_URL, headers=_openai_headers(auth))
        if response.status_code != 200:
            return AccountResult(
                email="?",
                usage=None,
                api_keys=[],
                error=f"OpenAI usage returned HTTP {response.status_code}",
            )

        payload_value = response.json()
        if not isinstance(payload_value, dict):
            return AccountResult(
                email="?",
                usage=None,
                api_keys=[],
                error="OpenAI usage response was invalid",
            )
        payload = cast(dict[str, object], payload_value)
        rate_limit_value = payload.get("rate_limit")
        if not isinstance(rate_limit_value, dict):
            return AccountResult(
                email="?",
                usage=None,
                api_keys=[],
                error="OpenAI usage response has no rate limit",
            )

        rate_limit = cast(dict[str, object], rate_limit_value)
        primary = _openai_usage_item(rate_limit.get("primary_window"))
        secondary = _openai_usage_item(rate_limit.get("secondary_window"))
        email_value = payload.get("email")
        email = email_value if isinstance(email_value, str) else "?"
        if not primary:
            return AccountResult(
                email=email,
                usage=None,
                api_keys=[],
                error="OpenAI usage response has no primary window",
            )
        return AccountResult(
            email=email,
            usage=AccountUsage(
                email=email, rolling=primary, weekly=secondary, monthly=None
            ),
            api_keys=[],
        )
    except (
        httpx.HTTPError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        return AccountResult(
            email="?", usage=None, api_keys=[], error=f"OpenAI usage failed: {error}"
        )
