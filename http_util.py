"""Small shared HTTP helpers: rate limiter + GET with retry/backoff.

Secrets never go through this module's logging (only URLs of public endpoints).
"""
import logging
import threading
import time

import requests

log = logging.getLogger("binance_gems.http")


class ExchangeError(Exception):
    """Raised when an exchange request fails after all retries."""


class RateLimiter:
    """Thread-safe minimum-interval limiter (requests per second)."""

    def __init__(self, rps):
        self.interval = 1.0 / rps if rps and rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


def get_json(session, url, params=None, limiter=None, timeout=12,
             max_attempts=4, sleep=time.sleep):
    """GET JSON with retry on network errors, HTTP 418/429 and 5xx.

    Honors Retry-After. Non-retryable 4xx raise immediately.
    """
    last = None
    for attempt in range(1, max_attempts + 1):
        if limiter:
            limiter.wait()
        wait = min(2 ** attempt, 20)
        try:
            resp = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last = ExchangeError(f"network error for {url}: {type(exc).__name__}")
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise ExchangeError(f"invalid JSON from {url}") from exc
            last = ExchangeError(f"HTTP {resp.status_code} for {url}")
            if not (resp.status_code in (418, 429) or resp.status_code >= 500):
                raise last
            retry_after = resp.headers.get("Retry-After", "")
            try:
                wait = min(float(retry_after), 60.0)
            except ValueError:
                pass
        if attempt < max_attempts:
            log.warning("%s (attempt %d/%d) - retrying in %.1fs", last, attempt, max_attempts, wait)
            sleep(wait)
    raise last
