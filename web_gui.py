import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import httpx
import webview
from webview.window import FixPoint

from module import (
    AccountResult,
    authorize_openai,
    fetch_account,
    fetch_grok_account,
    fetch_openai_account,
    get_xai_user_info,
    poll_xai_device_token,
    start_xai_device_auth,
    _extract_email_from_id_token,
    _is_grok_bot_account,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("LOCALAPPDATA", BASE_DIR)) / "UsageTracker"
CONFIG_FILE = DATA_DIR / "accounts.json"
OPENAI_AUTH_FILE = DATA_DIR / "openai-auth.json"
USAGE_CACHE_FILE = DATA_DIR / "usage-cache.json"
MEME_DISMISSED_FILE = DATA_DIR / "meme-dismissed.json"
OPENCODE_AUTH_FILE = Path.home() / ".local" / "share" / "opencode" / "auth.json"
UI_FILE = BASE_DIR / "ui" / "index.html"


class Api:
    def __init__(self):
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self.accounts = self._load_accounts()
        imported_openai = self._import_cli_openai_auth()
        imported_grok = self._import_xai_auth()
        self._results = self._load_usage_cache()
        if imported_openai or imported_grok:
            self._results = []
            self._save_accounts()
            self._save_usage_cache()
        self._last_keys: list[list[str]] = []
        self._window = None

    def bind_window(self, window):
        self._window = window

    def minimize_window(self):
        window = self._window
        if window is None:
            raise RuntimeError("Window is not ready")
        window.minimize()

    def close_window(self):
        window = self._window
        if window is None:
            raise RuntimeError("Window is not ready")
        window.destroy()

    def resize_window(self, width: int, height: int, edge: str):
        window = self._window
        if window is None:
            raise RuntimeError("Window is not ready")

        fix_points = {
            "n": FixPoint.SOUTH,
            "s": FixPoint.NORTH,
            "w": FixPoint.EAST,
            "e": FixPoint.WEST,
            "nw": FixPoint.SOUTH | FixPoint.EAST,
            "ne": FixPoint.SOUTH | FixPoint.WEST,
            "sw": FixPoint.NORTH | FixPoint.EAST,
            "se": FixPoint.NORTH | FixPoint.WEST,
        }
        if edge not in fix_points:
            raise ValueError("Invalid resize edge")
        window.resize(width, height, fix_points[edge])

    def _load_accounts(self):
        if not CONFIG_FILE.exists():
            return []
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            return json.load(file)

    def _save_accounts(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with CONFIG_FILE.open("w", encoding="utf-8") as file:
            json.dump(self.accounts, file, indent=2)
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass

    def _import_cli_openai_auth(self):
        if not OPENAI_AUTH_FILE.exists():
            return False
        try:
            auth = json.loads(OPENAI_AUTH_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False

        account_id = auth.get("accountId")
        required = ("access", "refresh", "expires", "accountId")
        if not isinstance(account_id, str) or not all(
            auth.get(key) for key in required
        ):
            return False
        if any(
            account.get("auth", {}).get("accountId") == account_id
            for account in self.accounts
        ):
            return False

        self.accounts.append({"type": "openai", "auth": auth})
        return True

    def _import_xai_auth(self):
        try:
            opencode_auth = self._load_opencode_auth()
        except RuntimeError:
            return False
        xai = opencode_auth.get("xai", {})
        if (
            not isinstance(xai, dict)
            or xai.get("type") != "oauth"
            or not xai.get("access")
        ):
            return False
        if any(
            account.get("type") == "grok"
            and account.get("auth", {}).get("access") == xai.get("access")
            for account in self.accounts
        ):
            return False
        auth_data = {
            "type": "oauth",
            "access": xai["access"],
            "refresh": xai.get("refresh"),
            "expires": xai.get("expires"),
        }
        email = None
        if "id_token" in xai:
            email = _extract_email_from_id_token(xai.get("id_token"))
        if not email:
            ui = get_xai_user_info(xai["access"])
            if ui:
                email = ui.get("email")
        if _is_grok_bot_account(xai):
            email = "BOT FLAG"
        if email:
            auth_data["email"] = email
        self.accounts.append({"type": "grok", "auth": auth_data})
        return True

    def _load_usage_cache(self):
        if not USAGE_CACHE_FILE.exists():
            return []
        try:
            cache = json.loads(USAGE_CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return cache if isinstance(cache, list) else []

    def _save_usage_cache(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with USAGE_CACHE_FILE.open("w", encoding="utf-8") as file:
            json.dump(self._results, file, indent=2)

    def should_show_meme(self):
        try:
            dismissed_at = json.loads(
                MEME_DISMISSED_FILE.read_text(encoding="utf-8")
            ).get("dismissed_at")
        except (OSError, json.JSONDecodeError, AttributeError):
            dismissed_at = None
        show = (
            not isinstance(dismissed_at, (int, float))
            or time.time() - dismissed_at >= 12 * 60 * 60
        )
        return {"ok": True, "show": show}

    def dismiss_meme(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with MEME_DISMISSED_FILE.open("w", encoding="utf-8") as file:
            json.dump({"dismissed_at": time.time()}, file)
        return {"ok": True}

    @staticmethod
    def _load_opencode_auth():
        try:
            auth = json.loads(OPENCODE_AUTH_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise RuntimeError(f"Could not read OpenCode auth: {error}") from error
        except json.JSONDecodeError as error:
            raise RuntimeError("OpenCode auth is invalid JSON") from error
        if not isinstance(auth, dict):
            raise RuntimeError("OpenCode auth is invalid")
        return auth

    @staticmethod
    def _save_opencode_auth(auth: dict):
        OPENCODE_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix="auth-", suffix=".json", dir=OPENCODE_AUTH_FILE.parent
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
                json.dump(auth, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, OPENCODE_AUTH_FILE)
            try:
                os.chmod(OPENCODE_AUTH_FILE, 0o600)
            except OSError:
                pass
        except OSError as error:
            raise RuntimeError(f"Could not update OpenCode auth: {error}") from error
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def list_accounts(self):
        with self._lock:
            try:
                opencode_auth = self._load_opencode_auth()
            except RuntimeError:
                opencode_auth = {}
            active_openai = opencode_auth.get("openai", {})
            active_account_id = (
                active_openai.get("accountId")
                if isinstance(active_openai, dict)
                else None
            )
            active_go = opencode_auth.get("opencode-go", {})
            active_go_key = (
                active_go.get("key") if isinstance(active_go, dict) else None
            )
            active_go_fingerprint = (
                hashlib.sha256(active_go_key.encode()).hexdigest()
                if isinstance(active_go_key, str)
                else None
            )
            active_xai = opencode_auth.get("xai", {})
            active_xai_email = (
                active_xai.get("email") if isinstance(active_xai, dict) else None
            )
            results = []
            for result in self._results:
                index = result.get("index")
                if (
                    not isinstance(index, int)
                    or index < 0
                    or index >= len(self.accounts)
                ):
                    continue
                provider = (
                    "ChatGPT"
                    if self.accounts[index].get("type") == "openai"
                    else "Grok"
                    if self.accounts[index].get("type") == "grok"
                    else "OpenCode Go"
                )
                fingerprints = result.get("goKeyFingerprints")
                if not isinstance(fingerprints, list):
                    fingerprints = []
                active_key_index = next(
                    (
                        key_index
                        for key_index, fingerprint in enumerate(fingerprints)
                        if isinstance(fingerprint, str)
                        and fingerprint == active_go_fingerprint
                    ),
                    None,
                )
                active = (
                    (
                        provider == "ChatGPT"
                        and self.accounts[index].get("auth", {}).get("accountId")
                        == active_account_id
                    )
                    or active_key_index is not None
                    or (
                        provider == "Grok"
                        and self.accounts[index].get("auth", {}).get("email")
                        == active_xai_email
                    )
                )
                results.append(
                    {
                        **result,
                        "provider": provider,
                        "activeInOpenCode": active,
                        "activeKeyIndex": active_key_index,
                    }
                )
            return {"ok": True, "count": len(self.accounts), "results": results}

    def switch_openai_auth(self, index: int):
        with self._lock:
            if index < 0 or index >= len(self.accounts):
                return {"ok": False, "error": "Invalid account"}
            account = self.accounts[index]
            auth = account.get("auth")
            required = ("access", "refresh", "expires", "accountId")
            if (
                account.get("type") != "openai"
                or not isinstance(auth, dict)
                or not all(auth.get(key) for key in required)
            ):
                return {"ok": False, "error": "OpenAI OAuth data is incomplete"}

            try:
                opencode_auth = self._load_opencode_auth()
                opencode_auth["openai"] = auth
                self._save_opencode_auth(opencode_auth)
            except RuntimeError as error:
                return {"ok": False, "error": str(error)}
            return {
                "ok": True,
                "provider": "openai",
                "message": "ChatGPT auth switched in OpenCode",
            }

    def switch_grok_auth(self, index: int):
        with self._lock:
            if index < 0 or index >= len(self.accounts):
                return {"ok": False, "error": "Invalid account"}
            account = self.accounts[index]
            auth = account.get("auth")
            if (
                account.get("type") != "grok"
                or not isinstance(auth, dict)
                or not auth.get("access")
            ):
                return {"ok": False, "error": "Grok OAuth data is incomplete"}

            try:
                opencode_auth = self._load_opencode_auth()
                opencode_auth["xai"] = auth
                self._save_opencode_auth(opencode_auth)
            except RuntimeError as error:
                return {"ok": False, "error": str(error)}
            return {
                "ok": True,
                "provider": "grok",
                "message": "Grok auth switched in OpenCode",
            }

    def add_cookie(self, cookie: str = ""):
        cookie = (cookie or "").strip()
        if not cookie:
            return {"ok": False, "error": "Cookie is required"}

        with self._lock:
            self.accounts.append({"cookie": cookie})
            self._save_accounts()
            return {
                "ok": True,
                "count": len(self.accounts),
                "message": "OpenCode cookie saved. Refresh to load usage.",
            }

    def add_openai_account(self):
        try:
            auth = authorize_openai()
        except Exception as error:
            return {
                "ok": False,
                "error": f"OpenAI login failed: {error}",
            }

        with self._lock:
            self.accounts.append({"type": "openai", "auth": auth})
            self._save_accounts()
            return {
                "ok": True,
                "count": len(self.accounts),
                "message": "ChatGPT is in. Refresh to load usage.",
            }

    def start_grok_device_auth(self):
        try:
            device = start_xai_device_auth()
            return {
                "ok": True,
                "device_code": device["device_code"],
                "user_code": device["user_code"],
                "verification_uri": device.get("verification_uri"),
                "verification_uri_complete": device.get("verification_uri_complete"),
                "expires_in": device.get("expires_in"),
                "interval": device.get("interval"),
            }
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def complete_grok_device_auth(self, device: dict):
        try:
            tokens = poll_xai_device_token(device)
            user_info = get_xai_user_info(tokens["access_token"]) or {}
            email = user_info.get("email") or _extract_email_from_id_token(
                tokens.get("id_token")
            )
            if _is_grok_bot_account({"access": tokens.get("access_token")}):
                email = "BOT FLAG"
            auth = {
                "type": "oauth",
                "access": tokens["access_token"],
                "refresh": tokens.get("refresh_token"),
                "id_token": tokens.get("id_token"),
                "expires": int(time.time() * 1000)
                + int(tokens.get("expires_in", 3600) * 1000),
                "email": email,
            }
            with self._lock:
                self.accounts.append({"type": "grok", "auth": auth})
                self._save_accounts()
            return {
                "ok": True,
                "count": len(self.accounts),
                "message": "Grok connected via device auth. Refresh to load usage.",
            }
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def remove_account(self, index: int):
        with self._lock:
            if index < 0 or index >= len(self.accounts):
                return {"ok": False, "error": "Invalid account"}
            self.accounts.pop(index)
            self._last_keys = []
            results = []
            for result in self._results:
                result_index = result.get("index")
                if not isinstance(result_index, int) or result_index == index:
                    continue
                results.append(
                    {
                        **result,
                        "index": result_index - 1
                        if result_index > index
                        else result_index,
                    }
                )
            self._results = results
            self._save_accounts()
            self._save_usage_cache()
            return {"ok": True, "count": len(self.accounts)}

    def copy_api_key(self, account_index: int, key_index: int):
        with self._lock:
            if account_index < 0 or account_index >= len(self._last_keys):
                return {"ok": False, "error": "Refresh accounts before copying a key"}
            keys = self._last_keys[account_index]
            if key_index < 0 or key_index >= len(keys):
                return {"ok": False, "error": "API key is no longer available"}
            return {"ok": True, "key": keys[key_index]}

    @staticmethod
    def _result_to_json(index: int, account: dict, result: AccountResult):
        usage = result.usage
        return {
            "index": index,
            "provider": "ChatGPT"
            if account.get("type") == "openai"
            else "Grok"
            if account.get("type") == "grok"
            else "OpenCode Go",
            "email": result.email,
            "rolling": [usage.rolling.percent, usage.rolling.reset_in_sec]
            if usage and usage.rolling
            else None,
            "weekly": [usage.weekly.percent, usage.weekly.reset_in_sec]
            if usage and usage.weekly
            else None,
            "monthly": [usage.monthly.percent, usage.monthly.reset_in_sec]
            if usage and usage.monthly
            else None,
            "keys": [f"..{key[-4:]}" for key in result.api_keys],
            "goKeyFingerprints": [
                hashlib.sha256(key.encode()).hexdigest() for key in result.api_keys
            ],
            "error": result.error,
        }

    async def _refresh(self, accounts):
        limits = httpx.Limits(
            max_keepalive_connections=10, max_connections=20, keepalive_expiry=30
        )
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30, limits=limits
        ) as client:
            account_results = await asyncio.gather(
                *(
                    fetch_grok_account(client, account)
                    if account.get("type") == "grok"
                    else fetch_openai_account(client, account.get("auth", {}))
                    if account.get("type") == "openai"
                    else fetch_account(client, account.get("cookie", ""))
                    for account in accounts
                )
            )

        results = [
            self._result_to_json(index, account, result)
            for index, (account, result) in enumerate(
                zip(accounts, account_results, strict=True)
            )
        ]
        return results, [result.api_keys for result in account_results]

    def refresh_all(self):
        with self._lock:
            accounts = json.loads(json.dumps(self.accounts))

        with self._refresh_lock:
            results, keys = asyncio.run(self._refresh(accounts))
        with self._lock:
            self._last_keys = keys
            self.accounts = accounts
            self._results = results
            self._save_accounts()
            self._save_usage_cache()
        return self.list_accounts()


if __name__ == "__main__":
    debug = os.getenv("PYWEBVIEW_DEBUG") == "1"
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = debug
    if debug:
        webview.settings["REMOTE_DEBUGGING_PORT"] = 9222

    api = Api()
    window = webview.create_window(
        "Usage Tracker",
        str(UI_FILE),
        js_api=api,
        width=1280,
        height=800,
        min_size=(680, 480),
        frameless=True,
        easy_drag=False,
        background_color="#ffffff",
        shadow=True,
    )
    api.bind_window(window)
    webview.start(debug=debug)
