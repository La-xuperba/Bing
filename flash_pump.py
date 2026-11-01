"""FLASH PUMP detector - independent from the early-pump scanner.

Calculation method (documented, deterministic)
----------------------------------------------
Data: 1-minute candles from the exchange (Binance Spot klines / Gate.io Spot
candlesticks), timestamps are exchange timestamps (open time of each candle).

For a window of N minutes (N in 5, 15, 30):

  end_price   = close of the LATEST 1m candle. This candle is usually still
                forming, so its close is simply the latest traded price. This
                is intentional (we want the live move) and documented here.
  window_start = open_time(latest candle) - (N-1) minutes
  start_price = open of the candle that starts at window_start.
                If that minute has no candle (no trades), the close of the
                last earlier candle is used (price carried forward, which is
                exactly the last traded price). If there is no earlier candle
                at all (new listing / too little history) the window is
                SKIPPED - never guessed.

  pct = (end_price / start_price - 1) * 100

The effective window length is therefore between N-1 and N minutes depending on
how many seconds of the current minute have elapsed.

A window is also skipped when: the latest candle is stale, any price is
invalid (<=0, NaN, inf), or the result is implausible (> MAX_PLAUSIBLE_PCT).

Lossless pre-filter: a +T% rise inside the last 30 minutes implies
last >= low_24h * (1 + T/100), so pairs whose 24h ticker does not satisfy this
cannot qualify and their candles are not downloaded (saves thousands of calls).
No price / market-cap / volume floor is applied to alerts (Gate has an optional
GATE_MIN_QUOTE_VOLUME_24H, default 0 = off).
"""
import logging
import math
import os
import re
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from http_util import ExchangeError, RateLimiter, get_json

log = logging.getLogger("binance_gems.flash")

WINDOWS_MIN = (5, 15, 30)
MAX_PLAUSIBLE_PCT = 1000.0
MAX_STALE_SECONDS = 180
CANDLE_LIMIT = 40
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDD", "BUSD", "EUR", "TRY",
                "USDE", "PYUSD", "USD1", "RLUSD", "USDT"}
BINANCE_LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
GATE_LEVERAGED_RE = re.compile(r"\d+[LS]$")

Candle = namedtuple("Candle", "open_time_ms open high low close")
WindowChange = namedtuple("WindowChange", "window_min pct start_price end_price start_ms end_ms")


def _valid_price(x):
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def compute_window_change(candles, window_min, now_ms=None,
                          max_stale_s=MAX_STALE_SECONDS, max_plausible_pct=MAX_PLAUSIBLE_PCT):
    """Return (WindowChange | None, reason). `candles` sorted ascending 1m Candles."""
    if not candles:
        return None, "no candles"
    last = candles[-1]
    if not _valid_price(last.close):
        return None, "invalid last price"
    if now_ms is not None and now_ms - last.open_time_ms > (60 + max_stale_s) * 1000:
        return None, "stale data"
    window_start = last.open_time_ms - (window_min - 1) * 60_000
    start_price = None
    for c in candles:
        if c.open_time_ms == window_start:
            start_price = c.open
            break
    if start_price is None:
        earlier = [c for c in candles if c.open_time_ms < window_start]
        if not earlier:
            return None, "insufficient history (new listing or missing candles)"
        start_price = earlier[-1].close  # no trades in between -> carry price forward
    if not _valid_price(start_price):
        return None, "invalid start price"
    pct = (last.close / start_price - 1.0) * 100.0
    if not math.isfinite(pct) or pct > max_plausible_pct:
        return None, "implausible change"
    return WindowChange(window_min, pct, float(start_price), float(last.close),
                        window_start, last.open_time_ms), "ok"


def detect_flash(candles, threshold_pct=5.0, windows=WINDOWS_MIN, now_ms=None):
    """Return (hits, skipped). A hit is a WindowChange with pct >= threshold."""
    hits, skipped = [], []
    for w in windows:
        wc, reason = compute_window_change(candles, w, now_ms=now_ms)
        if wc is None:
            skipped.append((w, reason))
        elif round(wc.pct, 6) >= threshold_pct:
            hits.append(wc)
    return hits, skipped


def build_alert(exchange, pair, hits, detected_at=None):
    """Create an informational alert dict. NEVER contains score or serial."""
    primary = max(hits, key=lambda h: h.pct)
    detected_at = detected_at or datetime.now(timezone.utc)
    return {
        "kind": "flash",
        "exchange": exchange,
        "symbol": pair["symbol"],
        "base": pair["base"],
        "quote": pair["quote"],
        "price": primary.end_price,
        "windows": {str(h.window_min): round(h.pct, 4) for h in hits},
        "primary_window": primary.window_min,
        "primary_pct": round(primary.pct, 4),
        "reference_price": primary.start_price,
        "window_end_ms": primary.end_ms,
        "detected_at": detected_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "detected_at_ts": detected_at.timestamp(),
    }


# --------------------------------------------------------------------------- clients
class BinanceSpotClient:
    name = "Binance"
    quote = "USDT"

    def __init__(self, session=None, rps=None):
        bases = [os.getenv("BINANCE_BASE_URL", "https://data-api.binance.vision").rstrip("/"),
                 "https://data-api.binance.vision", "https://api.binance.com"]
        self.bases = list(dict.fromkeys(bases))
        self.session = session or requests.Session()
        self.limiter = RateLimiter(rps or float(os.getenv("BINANCE_RPS", "12")))

    def _get(self, endpoint, params=None):
        last = None
        for base in self.bases:
            try:
                return get_json(self.session, base + endpoint, params, self.limiter, max_attempts=3)
            except ExchangeError as exc:
                last = exc
        raise last

    def server_time_ms(self):
        return int(self._get("/api/v3/time")["serverTime"])

    def list_pairs(self):
        info = self._get("/api/v3/exchangeInfo")
        tickers = {t["symbol"]: t for t in self._get("/api/v3/ticker/24hr")}
        pairs = []
        for it in info.get("symbols", []):
            sym, base = it.get("symbol", ""), it.get("baseAsset", "")
            if it.get("status") != "TRADING" or not it.get("isSpotTradingAllowed", False):
                continue
            if it.get("quoteAsset") != self.quote or base in STABLE_BASES:
                continue
            if sym.endswith(BINANCE_LEVERAGED_SUFFIXES):
                continue
            t = tickers.get(sym)
            if not t:
                continue
            try:
                last, low = float(t["lastPrice"]), float(t["lowPrice"])
            except (KeyError, TypeError, ValueError):
                continue
            pairs.append({"exchange": self.name, "symbol": sym, "base": base, "quote": self.quote,
                          "last": last, "low_24h": low,
                          "quote_volume_24h": float(t.get("quoteVolume") or 0)})
        return pairs

    def candles(self, pair, limit=CANDLE_LIMIT):
        raw = self._get("/api/v3/klines", {"symbol": pair["symbol"], "interval": "1m", "limit": limit})
        return parse_binance_klines(raw)


def parse_binance_klines(raw):
    out = []
    for r in raw or []:
        try:
            out.append(Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
        except (IndexError, TypeError, ValueError):
            continue
    out.sort(key=lambda c: c.open_time_ms)
    return out


class GateSpotClient:
    name = "Gate.io"
    quote = "USDT"
    base_url = "https://api.gateio.ws/api/v4"

    def __init__(self, session=None, rps=None):
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.limiter = RateLimiter(rps or float(os.getenv("GATE_RPS", "12")))
        self.min_quote_volume = float(os.getenv("GATE_MIN_QUOTE_VOLUME_24H", "0"))

    def _get(self, endpoint, params=None):
        return get_json(self.session, self.base_url + endpoint, params, self.limiter, max_attempts=3)

    def server_time_ms(self):
        return int(self._get("/spot/time")["server_time"])

    def list_pairs(self):
        meta = {p["id"]: p for p in self._get("/spot/currency_pairs")}
        pairs = []
        for t in self._get("/spot/tickers"):
            pid = t.get("currency_pair", "")
            m = meta.get(pid)
            if not m or m.get("trade_status") != "tradable" or m.get("quote") != self.quote:
                continue
            base = m.get("base", "")
            if base in STABLE_BASES or GATE_LEVERAGED_RE.search(base):
                continue
            try:
                last, low = float(t["last"]), float(t["low_24h"])
                qv = float(t.get("quote_volume") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if qv < self.min_quote_volume:
                continue
            pairs.append({"exchange": self.name, "symbol": pid, "base": base, "quote": self.quote,
                          "last": last, "low_24h": low, "quote_volume_24h": qv})
        return pairs

    def candles(self, pair, limit=CANDLE_LIMIT):
        raw = self._get("/spot/candlesticks",
                        {"currency_pair": pair["symbol"], "interval": "1m", "limit": limit})
        return parse_gate_candles(raw)


def parse_gate_candles(raw):
    """Gate row: [ts_sec, quote_vol, close, high, low, open, base_vol, window_closed]."""
    out = []
    for r in raw or []:
        try:
            out.append(Candle(int(float(r[0])) * 1000, float(r[5]), float(r[3]), float(r[4]), float(r[2])))
        except (IndexError, TypeError, ValueError):
            continue
    out.sort(key=lambda c: c.open_time_ms)
    return out


# --------------------------------------------------------------------------- scanning
def prefilter(pairs, threshold_pct, slack=0.9, max_candidates=400):
    """Lossless-ish prefilter (see module docstring). Invalid lows stay candidates."""
    need = 1.0 + threshold_pct * slack / 100.0
    cands = []
    for p in pairs:
        if not _valid_price(p["last"]):
            continue
        if not _valid_price(p["low_24h"]) or p["last"] >= p["low_24h"] * need:
            ratio = p["last"] / p["low_24h"] if _valid_price(p["low_24h"]) else float("inf")
            cands.append((ratio, p))
    cands.sort(key=lambda x: -x[0])
    return [p for _, p in cands[:max_candidates]]


def scan_exchange(client, threshold_pct=5.0, max_workers=6, now_ms=None):
    """Scan one exchange. Never raises: API failures are reported in stats."""
    stats = {"exchange": client.name, "pairs": 0, "candidates": 0, "checked": 0,
             "skipped": 0, "errors": 0, "alerts": 0, "fatal": None}
    try:
        pairs = client.list_pairs()
    except Exception as exc:  # noqa: BLE001 - one exchange must not kill the other
        log.error("%s: pair listing failed: %s", client.name, exc)
        stats["fatal"] = str(exc)
        return [], stats
    stats["pairs"] = len(pairs)
    cands = prefilter(pairs, threshold_pct)
    stats["candidates"] = len(cands)
    if now_ms is None:
        try:
            now_ms = client.server_time_ms()
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: server time failed (%s); using local clock", client.name, exc)
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    alerts = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(client.candles, p): p for p in cands}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                candles = fut.result()
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                log.warning("%s %s: candle fetch failed: %s", client.name, p["symbol"], exc)
                continue
            stats["checked"] += 1
            hits, skipped = detect_flash(candles, threshold_pct, now_ms=now_ms)
            if skipped and not hits:
                stats["skipped"] += 1
            if hits:
                alerts.append(build_alert(client.name, p, hits))
    stats["alerts"] = len(alerts)
    return alerts, stats
