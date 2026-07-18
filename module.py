import asyncio
import base64
import binascii
import hashlib
import html
import json
import os
import re
import secrets
import struct
import threading
import time
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
GROK_USAGE_URL = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_DEVICE_URL = "https://auth.x.ai/oauth2/device/code"
XAI_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_DEVICE_DEFAULT_INTERVAL_S = 5
XAI_DEVICE_MIN_INTERVAL_S = 1
XAI_DEVICE_SLOW_DOWN_INCREMENT_S = 5
XAI_DEVICE_DEFAULT_EXPIRES_S = 5 * 60
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


def _grok_headers(cookie: str = "", bearer: str = "") -> dict:
    h = {
        "Content-Type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    }
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    if cookie:
        h["Cookie"] = cookie
    return h


def _parse_varint(buf: bytes, pos: int):
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    return result, pos


def _read_field(buf: bytes, pos: int):
    tag, pos = _parse_varint(buf, pos)
    fn = tag >> 3
    w = tag & 0x7
    if w == 0:
        v, pos = _parse_varint(buf, pos)
        return fn, "var", v, pos
    if w == 5:
        v = struct.unpack("<I", buf[pos : pos + 4])[0]
        pos += 4
        return fn, "f32", v, pos
    if w == 2:
        ln, pos = _parse_varint(buf, pos)
        v = buf[pos : pos + ln]
        pos += ln
        return fn, "len", v, pos
    return fn, "?", None, pos


def _get_float(raw: int) -> float:
    return struct.unpack("<f", struct.pack("<I", raw))[0]


def _extract_email_from_id_token(id_token: str | None) -> str | None:
    if not id_token or not isinstance(id_token, str):
        return None
    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        claims = json.loads(decoded)
        return claims.get("email")
    except Exception:
        return None


def get_xai_user_info(access_token: str) -> dict | None:
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                "https://auth.x.ai/oauth2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.is_success:
                return resp.json()
    except Exception:
        pass
    return None


async def get_xai_user_info_async(access_token: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://auth.x.ai/oauth2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.is_success:
                return resp.json()
    except Exception:
        pass
    return None


def _is_grok_bot_account(auth: dict) -> bool:
    try:
        access = auth.get("access", "")
        if not access:
            return False
        parts = access.split(".")
        if len(parts) < 2:
            return False
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return bool(claims.get("bot_flag_source"))
    except Exception:
        return False


def _parse_ts(buf: bytes):
    p = 0
    sec = None
    nano = 0
    while p < len(buf):
        fn, w, v, p = _read_field(buf, p)
        if fn == 1 and w == "var":
            sec = v
        elif fn == 2 and w == "var":
            nano = v
    if sec is None:
        return None
    return datetime.fromtimestamp(sec + nano / 1e9, tz=timezone.utc)


def _parse_grok_usage(body: bytes) -> UsageItem | None:
    try:
        if len(body) < 5 or body[0] != 0:
            return None
        length = int.from_bytes(body[1:5], "big")
        if len(body) < 5 + length:
            return None
        proto = body[5 : 5 + length]
        p = 0
        cfg = None
        while p < len(proto):
            fn, w, v, p = _read_field(proto, p)
            if fn == 1 and w == "len":
                cfg = v
                break
        if not cfg:
            return None
        p = 0
        pct = None
        end_dt = None
        while p < len(cfg):
            fn, w, v, p = _read_field(cfg, p)
            if fn == 1 and w == "f32":
                pct = _get_float(v)
            elif fn == 8 and w == "len":
                pp = 0
                while pp < len(v):
                    f2, w2, v2, pp = _read_field(v, pp)
                    if f2 == 3 and w2 == "len":
                        end_dt = _parse_ts(v2)
        if pct is None:
            return None
        reset = 0
        if end_dt:
            delta = (end_dt - datetime.now(timezone.utc)).total_seconds()
            reset = max(0, int(delta))
        return UsageItem(percent=round(pct), reset_in_sec=reset)
    except Exception:
        return None


async def _refresh_xai_token(
    client: httpx.AsyncClient, refresh_token: str
) -> dict | None:
    try:
        resp = await client.post(
            XAI_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": XAI_CLIENT_ID,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def request_xai_device_code() -> dict:
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            XAI_DEVICE_URL,
            data={"client_id": XAI_CLIENT_ID, "scope": XAI_SCOPE},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        if not resp.is_success:
            detail = resp.text[:200]
            raise RuntimeError(
                f"xAI device code request failed ({resp.status_code}){detail}"
            )
        data = resp.json()
        if (
            not data.get("device_code")
            or not data.get("user_code")
            or not data.get("verification_uri")
        ):
            raise RuntimeError("Invalid device code response from xAI")
        return data


def start_xai_device_auth() -> dict:
    device = request_xai_device_code()
    url = device.get("verification_uri_complete") or device.get("verification_uri")
    if url:
        webbrowser.open(url)
    return device


def poll_xai_device_token(device: dict) -> dict:
    device_code = device.get("device_code")
    if not device_code:
        raise RuntimeError("Missing device_code")
    interval = max(
        int(device.get("interval", XAI_DEVICE_DEFAULT_INTERVAL_S)),
        XAI_DEVICE_MIN_INTERVAL_S,
    )
    expires_in = int(device.get("expires_in", XAI_DEVICE_DEFAULT_EXPIRES_S))
    start = time.time()
    deadline = start + expires_in
    with httpx.Client(timeout=30) as client:
        while time.time() < deadline:
            resp = client.post(
                XAI_TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": XAI_CLIENT_ID,
                    "device_code": device_code,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
            )
            if resp.status_code == 200:
                return resp.json()
            try:
                body = resp.json()
                error = body.get("error")
            except Exception:
                error = None
            if error == "authorization_pending":
                time.sleep(interval)
                continue
            if error == "slow_down":
                interval += XAI_DEVICE_SLOW_DOWN_INCREMENT_S
                time.sleep(interval)
                continue
            if error in ("access_denied", "authorization_denied"):
                raise RuntimeError("xAI device authorization was denied")
            if error == "expired_token":
                raise RuntimeError("xAI device code expired - please re-run")
            detail = (
                (body.get("error_description") if isinstance(body, dict) else "")
                or error
                or resp.text[:100]
            )
            raise RuntimeError(f"xAI device token failed ({resp.status_code}){detail}")
        raise RuntimeError("xAI device authorization timed out")


async def fetch_grok_account(client: httpx.AsyncClient, account: dict) -> AccountResult:
    auth = account.get("auth") or {}
    cookie = account.get("cookie", "")
    access = auth.get("access")
    email = auth.get("email")
    if not email and access:
        ui = await get_xai_user_info_async(access)
        if ui:
            email = ui.get("email")
    if not email:
        email = "?"
    is_bot = _is_grok_bot_account(auth)
    if is_bot:
        email = "BOT FLAG"
    try:
        if access:
            expires = auth.get("expires", 0)
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            if expires and expires < now + 5 * 60 * 1000 and auth.get("refresh"):
                refreshed = await _refresh_xai_token(client, auth["refresh"])
                if refreshed and refreshed.get("access_token"):
                    auth["access"] = refreshed["access_token"]
                    access = refreshed["access_token"]
                    if "expires_in" in refreshed:
                        auth["expires"] = (
                            int(datetime.now(timezone.utc).timestamp() * 1000)
                            + int(refreshed["expires_in"]) * 1000
                        )
                    if "refresh_token" in refreshed:
                        auth["refresh"] = refreshed["refresh_token"]
            headers = _grok_headers(bearer=access)
        elif cookie:
            headers = _grok_headers(cookie=cookie)
        else:
            return AccountResult(
                email=email, usage=None, api_keys=[], error="No grok auth or cookie"
            )
        resp = await client.post(
            GROK_USAGE_URL, content=b"\x00\x00\x00\x00\x00", headers=headers
        )
        if resp.status_code != 200:
            return AccountResult(
                email=email,
                usage=None,
                api_keys=[],
                error=f"Grok usage returned HTTP {resp.status_code}",
            )
        usage = _parse_grok_usage(resp.content)
        if not usage:
            if is_bot:
                return AccountResult(
                    email=email,
                    usage=None,
                    api_keys=[],
                )
            return AccountResult(
                email=email,
                usage=None,
                api_keys=[],
                error="Grok usage parse failed or empty",
            )
        return AccountResult(
            email=email,
            usage=AccountUsage(email=email, rolling=usage, weekly=None, monthly=None),
            api_keys=[],
        )
    except (httpx.HTTPError, TypeError, ValueError) as error:
        return AccountResult(
            email=email, usage=None, api_keys=[], error=f"Grok usage failed: {error}"
        )
