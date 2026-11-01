"""Publishing adapters: Telegram (official Bot API) and Binance Square (text only).

Secrets are read from environment variables and are never logged. Exception
messages from `requests` can contain the Telegram URL (which embeds the bot
token), so they are never logged raw - only their class name / a redacted form.
"""
import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger("binance_gems.publish")

REQUEST_TIMEOUT = 15


class PermanentError(Exception):
    """Do not retry (bad token, bad chat id, banned, daily limit...)."""


class TemporaryError(Exception):
    """Retry later (network, 5xx, rate limit exhausted)."""


def redact(text, *secrets):
    out = str(text)
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out


# ----------------------------------------------------------------------------- Telegram
class TelegramPublisher:
    name = "telegram"

    def __init__(self, token=None, chat_id=None, session=None, sleep=time.sleep,
                 max_attempts=5, max_retry_after=120):
        self.token = (token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")).strip()
        self.session = session or requests.Session()
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.max_retry_after = max_retry_after

    @property
    def configured(self):
        return bool(self.token and self.chat_id)

    def send(self, text):
        """Send one message. Returns None on success, raises Permanent/TemporaryError."""
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        last = "unknown"
        for attempt in range(1, self.max_attempts + 1):
            wait = min(2 ** attempt, 30)
            try:
                resp = self.session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last = f"network error ({type(exc).__name__})"
            else:
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                if resp.status_code == 200 and body.get("ok"):
                    return None
                desc = redact(body.get("description", ""), self.token)
                if resp.status_code == 429:
                    retry_after = (body.get("parameters") or {}).get("retry_after", wait)
                    wait = min(float(retry_after) + 1, self.max_retry_after)
                    last = f"429 rate limited (retry_after={retry_after})"
                elif resp.status_code >= 500:
                    last = f"HTTP {resp.status_code}"
                else:
                    raise PermanentError(f"Telegram rejected the message: HTTP {resp.status_code} {desc}")
            if attempt < self.max_attempts:
                log.warning("Telegram: %s - retry %d/%d in %.0fs", last, attempt, self.max_attempts, wait)
                self.sleep(wait)
        raise TemporaryError(f"Telegram failed after {self.max_attempts} attempts: {last}")


# ----------------------------------------------------------------------------- Binance Square
SQUARE_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"
# Error codes documented in binance/binance-skills-hub (square-post skill).
SQUARE_PERMANENT_CODES = {"220003", "220004", "220010", "220011", "2000001", "2000002", "30008"}
SQUARE_DAILY_LIMIT_CODE = "220009"


class SquarePublisher:
    """Text-only Binance Square post via the documented OpenAPI endpoint.

    Request (as documented): POST {SQUARE_URL}
      headers: X-Square-OpenAPI-Key, Content-Type: application/json, clienttype: binanceSkill
      body:    {"bodyTextOnly": "<text>"}
    Success: JSON {"code": "000000", "data": {"id": "..."}}. Anything else is NOT
    reported as published.
    """
    name = "square"

    def __init__(self, api_key=None, session=None, url=None):
        self.api_key = (api_key if api_key is not None else os.getenv("BINANCE_SQUARE_API_KEY", "")).strip()
        self.url = url or os.getenv("BINANCE_SQUARE_API_URL", "").strip() or SQUARE_URL
        self.session = session or requests.Session()
        self.daily_limit_hit = False

    @property
    def configured(self):
        return bool(self.api_key) and self.api_key != "your_api_key"

    def publish(self, text):
        """Return post id (str) on confirmed success; raise otherwise."""
        if self.daily_limit_hit:
            raise PermanentError("Square daily OpenAPI post limit reached (220009)")
        headers = {"X-Square-OpenAPI-Key": self.api_key, "Content-Type": "application/json",
                   "clienttype": "binanceSkill"}
        try:
            resp = self.session.post(self.url, json={"bodyTextOnly": text}, headers=headers,
                                     timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise TemporaryError(f"Square network error ({type(exc).__name__})") from None
        if resp.status_code >= 500 or resp.status_code == 429:
            raise TemporaryError(f"Square HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise TemporaryError(f"Square returned non-JSON (HTTP {resp.status_code})") from None
        code = str(body.get("code"))
        if resp.status_code == 200 and code == "000000":
            post_id = (body.get("data") or {}).get("id")
            if not post_id:
                # Documented success always carries an id; without it we do not claim success.
                raise TemporaryError("Square reported success without a post id; not counted as published")
            return str(post_id)
        msg = redact(body.get("message") or "", self.api_key)
        if code == SQUARE_DAILY_LIMIT_CODE:
            self.daily_limit_hit = True
            raise PermanentError("Square daily OpenAPI post limit reached (220009)")
        if code in SQUARE_PERMANENT_CODES:
            raise PermanentError(f"Square error {code}: {msg}")
        raise TemporaryError(f"Square error code={code} http={resp.status_code}: {msg}")


# ----------------------------------------------------------------------------- drafts
def append_draft(path, item, reason):
    """Save a text post to the draft file (used when Square is not configured/failed)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(f"--- {item['kind'].upper()} {item['id']} | reason: {reason} ---\n{item['text']}\n\n")
