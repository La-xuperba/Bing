"""
BINANCE GEMS v2 - Early-Pump Signals + Flash Pump Alerts
Python 3.11 | pip install -r requirements.txt

Two logically independent systems share one publishing queue:

  1. EARLY-PUMP SIGNALS  - the ORIGINAL scanner (functions below marked
     "ORIGINAL" are unchanged: same indicators, score, filters, eligibility).
  2. FLASH PUMP ALERTS   - flash_pump.py (+5% in 5/15/30 min on Binance & Gate.io
     Spot). Informational only: no score, no signal serial.

Every coin is published as its own Telegram message / Binance Square text post,
10-20 s apart. Detection never waits for publishing; unfinished posts stay in
the persisted queue (state/state.json) for the next run.

This bot never places orders.
"""
import argparse
import logging
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

import state_store as ss
from flash_pump import BinanceSpotClient, GateSpotClient, scan_exchange
from post_copy import render_flash, render_signal
from publishers import (PermanentError, SquarePublisher, TelegramPublisher,
                        TemporaryError, append_draft)

# ============================ ORIGINAL SETTINGS (unchanged) ============================
BASE_URL = os.getenv("BINANCE_BASE_URL", "https://data-api.binance.vision").rstrip("/")
BINANCE_URLS = list(dict.fromkeys([BASE_URL, "https://data-api.binance.vision", "https://api.binance.com"]))
QUOTE_ASSET = "USDT"
MIN_PRICE = 0.01
MAX_24H_CHANGE_PCT = 15.0
TREND_INTERVAL = "1h"
TRIGGER_INTERVAL = "15m"
TREND_LIMIT = 120
TRIGGER_LIMIT = 100
BTC_RSI_MIN = 48
COIN_RSI_MIN = 52
COIN_RSI_MAX = 67
MIN_VOLUME_RATIO = 1.5
PREVIOUS_VOLUME_RATIO = 1.2
MIN_SCORE = int(os.getenv("MIN_SCORE", "45"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))
REQUEST_TIMEOUT = 12

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("binance_gems")


# ============================ ORIGINAL EARLY-PUMP SCANNER (unchanged) ============================
def binance_get(endpoint, params=None):
    last_error = None
    for base in BINANCE_URLS:
        try:
            response = requests.get(base + endpoint, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            log.warning("Binance endpoint failed (%s): %s", base, exc)
    raise last_error


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df, period=14):
    previous = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - previous).abs(),
        (df["low"] - previous).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def macd_histogram(close):
    macd = ema(close, 12) - ema(close, 26)
    return macd - ema(macd, 9)


def get_klines(symbol, interval, limit):
    raw = binance_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "number_of_trades", "taker_buy_base_volume",
            "taker_buy_quote_volume", "ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for col in ["open", "high", "low", "close", "volume", "quote_volume",
                "number_of_trades", "taker_buy_base_volume", "taker_buy_quote_volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def build_universe():
    info = binance_get("/api/v3/exchangeInfo")
    tickers = binance_get("/api/v3/ticker/24hr")
    by_symbol = {t["symbol"]: t for t in tickers}
    stable_bases = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDD", "BUSD",
                    "EUR", "TRY", "USDE", "PYUSD"}
    excluded_suffixes = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
    result = []
    for item in info["symbols"]:
        symbol, base = item["symbol"], item["baseAsset"]
        if item.get("status") != "TRADING" or not item.get("isSpotTradingAllowed", False):
            continue
        if item.get("quoteAsset") != QUOTE_ASSET or base in stable_bases:
            continue
        if any(symbol.endswith(suffix) for suffix in excluded_suffixes):
            continue
        ticker = by_symbol.get(symbol)
        if not ticker:
            continue
        price = float(ticker["lastPrice"])
        change = float(ticker["priceChangePercent"])
        if price < MIN_PRICE or change >= MAX_24H_CHANGE_PCT:
            continue
        result.append({
            "symbol": symbol, "base_asset": base, "last_price": price,
            "quote_volume_24h": float(ticker["quoteVolume"]),
            "price_change_24h_pct": change,
        })
    return result


def get_btc_return_6h():
    df = get_klines("BTCUSDT", "1h", TREND_LIMIT)
    df["ema20"], df["ema50"] = ema(df["close"], 20), ema(df["close"], 50)
    df["rsi14"] = rsi(df["close"])
    row = df.iloc[-2]
    return float(df["close"].pct_change(6).iloc[-2] * 100), bool(
        row["close"] > row["ema50"] and row["ema20"] > row["ema50"] and row["rsi14"] >= BTC_RSI_MIN
    )


def screen_symbol(item, btc_return):
    trend_df = get_klines(item["symbol"], TREND_INTERVAL, TREND_LIMIT)
    trigger_df = get_klines(item["symbol"], TRIGGER_INTERVAL, TRIGGER_LIMIT)
    trend_df["ema20"] = ema(trend_df["close"], 20)
    trend_df["ema50"] = ema(trend_df["close"], 50)
    trend_df["return_6h"] = trend_df["close"].pct_change(6) * 100

    trigger_df["rsi14"] = rsi(trigger_df["close"])
    trigger_df["atr14"] = atr(trigger_df)
    trigger_df["macd_histogram"] = macd_histogram(trigger_df["close"])
    trigger_df["volume_sma20"] = trigger_df["volume"].rolling(20).mean()
    trigger_df["volume_ratio"] = trigger_df["volume"] / trigger_df["volume_sma20"]
    trigger_df["previous_high_20"] = trigger_df["high"].shift(1).rolling(20).max()
    trigger_df["body"] = (trigger_df["close"] - trigger_df["open"]).abs()
    trigger_df["upper_wick"] = trigger_df["high"] - trigger_df[["open", "close"]].max(axis=1)
    trigger_df["wick_ratio"] = trigger_df["upper_wick"] / trigger_df["body"].replace(0, np.nan)

    trend, trigger, previous = trend_df.iloc[-2], trigger_df.iloc[-2], trigger_df.iloc[-3]
    trend_ok = trend["close"] > trend["ema20"] > trend["ema50"]
    volume_ratio = float(trigger["volume_ratio"])
    previous_volume = float(previous["volume_ratio"])
    breakout_ok = trigger["close"] > trigger["previous_high_20"]
    momentum_ok = COIN_RSI_MIN <= trigger["rsi14"] <= COIN_RSI_MAX and trigger["macd_histogram"] > 0
    relative_strength = float(trend["return_6h"] - btc_return)
    distance = float(trend["close"] / trend["ema20"])
    score = 0
    reasons = []
    if trend_ok: score += 25; reasons.append("1h trend aligned")
    if volume_ratio >= MIN_VOLUME_RATIO: score += 20; reasons.append(f"15m volume {volume_ratio:.2f}x")
    if breakout_ok: score += 15; reasons.append("15m breakout")
    if momentum_ok: score += 15; reasons.append("RSI/MACD aligned")
    if relative_strength > 0: score += 15; reasons.append("outperforming BTC")
    if 1.01 <= distance <= 1.06: score += 10; reasons.append("EMA20 extension acceptable")

    green = trigger["close"] > trigger["open"]
    body_atr = float(trigger["body"] / trigger["atr14"]) if trigger["atr14"] > 0 else 0
    wick_ok = pd.notna(trigger["wick_ratio"]) and trigger["wick_ratio"] <= 1.5
    active = volume_ratio >= MIN_VOLUME_RATIO or previous_volume >= PREVIOUS_VOLUME_RATIO
    candle_ok = green and body_atr >= 0.25 and wick_ok
    eligible = score >= MIN_SCORE and (trend_ok or momentum_ok) and active and candle_ok and relative_strength > -1.0

    return {
        **item, "close": float(trigger["close"]), "score": int(score), "eligible": bool(eligible),
        "volume_ratio_15m": volume_ratio, "previous_volume_ratio_15m": previous_volume,
        "rsi_15m": float(trigger["rsi14"]), "relative_strength_vs_btc_pct": relative_strength,
        "reasons": " | ".join(reasons) if reasons else "Activity/momentum scan",
        "candle_close_time": trigger["close_time"],
        # v2 addition (read-only facts used by the post copy; no effect on eligibility):
        "trend_ok": bool(trend_ok), "breakout_ok": bool(breakout_ok), "momentum_ok": bool(momentum_ok),
    }


def scan():
    log.info("Loading BTC regime and Binance Spot universe...")
    btc_return, btc_enabled = get_btc_return_6h()
    if not btc_enabled:
        log.warning("BTC regime weak; scan continues with caution.")
    universe = build_universe()
    log.info("%d symbols in eligible Binance Spot universe", len(universe))
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(screen_symbol, item, btc_return): item["symbol"] for item in universe}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append((futures[future], str(exc)))
    if errors:
        log.warning("%d symbols failed; examples: %s", len(errors), errors[:5])
    if not results:
        return pd.DataFrame()
    df = pd.DataFrame(results).sort_values(
        ["eligible", "score", "relative_strength_vs_btc_pct", "volume_ratio_15m"],
        ascending=[False, False, False, False]
    )
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    df.to_csv(f"pre_pump_ranking_{stamp}.csv", index=False)
    return df[df["eligible"]].copy()


# ============================ v2: configuration ============================
def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


class Config:
    def __init__(self):
        self.state_path = os.getenv("BOT_STATE_PATH", "state/state.json")
        self.draft_path = os.getenv("BOT_DRAFT_PATH", "square_drafts.txt")
        self.flash_threshold = _env_float("FLASH_THRESHOLD_PCT", 5.0)
        self.flash_cooldown_s = _env_float("FLASH_COOLDOWN_MINUTES", 60) * 60
        self.signal_cooldown_s = _env_float("SIGNAL_COOLDOWN_MINUTES", 120) * 60
        self.signal_interval_s = _env_float("SIGNAL_SCAN_INTERVAL_MINUTES", 15) * 60
        self.flash_max_age_s = _env_float("FLASH_MAX_AGE_MINUTES", 45) * 60
        self.signal_max_age_s = _env_float("SIGNAL_MAX_AGE_MINUTES", 120) * 60
        self.delay_min = _env_float("POST_DELAY_MIN_SECONDS", 10)
        self.delay_max = _env_float("POST_DELAY_MAX_SECONDS", 20)
        self.max_runtime_s = _env_float("BOT_MAX_RUNTIME_SECONDS", 600)
        self.max_item_attempts = int(_env_float("MAX_ITEM_ATTEMPTS", 3))
        self.flash_workers = int(_env_float("FLASH_WORKERS", 6))
        self.exchanges = [x.strip().lower() for x in os.getenv("FLASH_EXCHANGES", "binance,gate").split(",") if x.strip()]
        self.force_signal_scan = os.getenv("FORCE_SIGNAL_SCAN", "").lower() in ("1", "true", "yes")
        self.dry_run = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")


# ============================ v2: detection -> post items (pure, testable) ============================
SIGNAL_FIELDS = ("symbol", "base_asset", "close", "score", "price_change_24h_pct", "quote_volume_24h",
                 "volume_ratio_15m", "rsi_15m", "relative_strength_vs_btc_pct", "reasons",
                 "trend_ok", "breakout_ok", "momentum_ok")


def eligible_signal_rows(df):
    """Rows that PASSED the original eligibility rules - nothing else becomes a signal."""
    if df is None or len(df) == 0 or "eligible" not in df.columns:
        return []
    rows = []
    for _, r in df[df["eligible"] == True].iterrows():  # noqa: E712
        d = {k: (r[k].item() if hasattr(r[k], "item") else r[k]) for k in SIGNAL_FIELDS if k in r}
        ts = r.get("candle_close_time")
        d["candle_close_ms"] = int(pd.Timestamp(ts).timestamp() * 1000) if ts is not None else None
        rows.append(d)
    return rows


def build_items(signal_rows, flash_alerts, state, now, cfg, rng=None):
    """Turn detections into rendered, de-duplicated post items.

    Signals and flash alerts are planned independently: a coin can yield one,
    both, or neither. Flash alerts never get a score or serial number.
    """
    rng = rng or random.Random()
    items = []
    for row in signal_rows:
        it = ss.plan_signal_item(row, state, now, cfg.signal_cooldown_s)
        if it:
            it["text"], idx = render_signal(it["data"], rng, state["last_template"])
            state["last_template"].update(idx)
            items.append(it)
    for alert in flash_alerts:
        it = ss.plan_flash_item(alert, state, now, cfg.flash_cooldown_s)
        if it:
            it["text"], idx = render_flash(it["data"], rng, state["last_template"])
            state["last_template"].update(idx)
            items.append(it)
    return items


# ============================ v2: publishing ============================
def publish_queue(state, cfg, telegram, square, sleep=time.sleep, rng=None,
                  deadline=None, save=None, clock=time.monotonic):
    """Publish queued items ONE AT A TIME with a random 10-20 s pause between items.

    State is saved after every channel result so a crash cannot re-send finished posts.
    Items not finished before the deadline stay in state['queue'].
    """
    rng = rng or random.Random()
    save = save or (lambda: None)
    stats = {"telegram": 0, "square": 0, "drafts": 0, "failed": 0, "left": 0}
    first = True
    queue = ss.ordered_queue(state)
    for n, item in enumerate(queue):
        if deadline is not None and clock() >= deadline:
            log.warning("Runtime budget reached; %d item(s) stay queued for the next run", len(queue) - n)
            break
        if not first:
            delay = rng.uniform(cfg.delay_min, cfg.delay_max)
            if deadline is not None and clock() + delay >= deadline:
                log.warning("Not enough runtime for the next post delay; %d item(s) stay queued", len(queue) - n)
                break
            log.info("Waiting %.1fs before next post", delay)
            sleep(delay)
        first = False
        _publish_item(item, cfg, telegram, square, stats)
        if ss.item_done(item):
            state["queue"] = [q for q in state["queue"] if q["id"] != item["id"]]
            state["recent_ids"].append(item["id"])
        save()
    stats["left"] = len(state["queue"])
    return stats


def _publish_item(item, cfg, telegram, square, stats):
    ch = item["channels"]
    if ch["telegram"] == "pending":
        try:
            telegram.send(item["text"])
            ch["telegram"] = "sent"
            stats["telegram"] += 1
        except PermanentError as exc:
            ch["telegram"] = "failed"; stats["failed"] += 1
            log.error("Telegram permanent failure for %s: %s", item["id"], exc)
        except TemporaryError as exc:
            item["attempts"]["telegram"] += 1
            log.warning("Telegram temporary failure for %s: %s", item["id"], exc)
            if item["attempts"]["telegram"] >= cfg.max_item_attempts:
                ch["telegram"] = "failed"; stats["failed"] += 1
    if ch["square"] == "pending":
        if not square.configured:
            append_draft(cfg.draft_path, item, "Square API key not configured")
            ch["square"] = "draft"; stats["drafts"] += 1
        else:
            try:
                post_id = square.publish(item["text"])
                ch["square"] = "published"; stats["square"] += 1
                log.info("Square post confirmed for %s (id %s)", item["id"], post_id)
            except PermanentError as exc:
                append_draft(cfg.draft_path, item, f"Square refused: {exc}")
                ch["square"] = "draft"; stats["drafts"] += 1
                log.error("Square permanent failure for %s: %s (saved as draft)", item["id"], exc)
            except TemporaryError as exc:
                item["attempts"]["square"] += 1
                log.warning("Square temporary failure for %s: %s", item["id"], exc)
                if item["attempts"]["square"] >= cfg.max_item_attempts:
                    append_draft(cfg.draft_path, item, f"Square failed repeatedly: {exc}")
                    ch["square"] = "draft"; stats["drafts"] += 1


# ============================ v2: orchestration ============================
def _flash_clients(cfg):
    clients = []
    if "binance" in cfg.exchanges: clients.append(BinanceSpotClient())
    if "gate" in cfg.exchanges: clients.append(GateSpotClient())
    return clients


def run_detection(cfg, state, now):
    """Fast phase: scan only. Returns (signal_rows, flash_alerts, problems)."""
    problems, signal_rows, flash_alerts = [], [], []
    if cfg.force_signal_scan or now - state["last_signal_scan_ts"] >= cfg.signal_interval_s - 90:
        try:
            signal_rows = eligible_signal_rows(scan())
            state["last_signal_scan_ts"] = now
            log.info("Early-pump scan: %d eligible coin(s)", len(signal_rows))
        except Exception as exc:  # noqa: BLE001 - flash system must still run
            log.error("Early-pump scan failed: %s", exc)
            problems.append("signal scan failed")
    else:
        log.info("Early-pump scan skipped (interval %.0f min not reached)", cfg.signal_interval_s / 60)
    clients = _flash_clients(cfg)
    with ThreadPoolExecutor(max_workers=max(1, len(clients))) as pool:
        futs = [pool.submit(scan_exchange, c, cfg.flash_threshold, cfg.flash_workers) for c in clients]
        for fut in futs:
            alerts, stats = fut.result()
            log.info("Flash scan %s: %s", stats["exchange"], {k: v for k, v in stats.items() if k != "exchange"})
            flash_alerts.extend(alerts)
            if stats["fatal"]:
                problems.append(f"flash scan {stats['exchange']} failed")
    return signal_rows, flash_alerts, problems


def run(cfg=None):
    cfg = cfg or Config()
    start = time.monotonic()
    now = time.time()
    state = ss.load_state(cfg.state_path)
    ss.prune(state, now, cfg.flash_cooldown_s, cfg.signal_cooldown_s)
    expired = ss.drop_expired(state, now, cfg.flash_max_age_s, cfg.signal_max_age_s)
    if expired:
        log.warning("Dropped %d expired queued post(s)", len(expired))

    telegram, square = TelegramPublisher(), SquarePublisher()
    if not telegram.configured:
        log.warning("Telegram secrets missing; Telegram posts are skipped.")
    if not square.configured:
        log.info("BINANCE_SQUARE_API_KEY not set; Square posts are saved to %s as drafts.", cfg.draft_path)

    signal_rows, flash_alerts, problems = run_detection(cfg, state, now)
    items = build_items(signal_rows, flash_alerts, state, now, cfg)
    added = sum(ss.enqueue(state, it, telegram.configured, True) for it in items)
    log.info("Detected: %d signal(s), %d flash alert(s) -> %d new post(s) queued, %d pending total",
             len(signal_rows), len(flash_alerts), added, len(state["queue"]))

    if cfg.dry_run:
        for it in ss.ordered_queue(state):
            print(f"\n===== {it['kind'].upper()} {it['id']} =====\n{it['text']}")
        log.info("DRY_RUN: nothing published, state not saved.")
        return 0

    ss.save_state(state, cfg.state_path)  # queue is durable BEFORE any publishing starts
    deadline = start + cfg.max_runtime_s
    stats = publish_queue(state, cfg, telegram, square, sleep=time.sleep, deadline=deadline,
                          save=lambda: ss.save_state(state, cfg.state_path))
    ss.save_state(state, cfg.state_path)
    log.info("Publish summary: %s", stats)
    if problems:
        log.error("Problems this run: %s", "; ".join(problems))
        return 1 if len(problems) >= 2 else 0
    return 0


def main():
    parser = argparse.ArgumentParser(description="BINANCE GEMS v2")
    parser.add_argument("--dry-run", action="store_true", help="detect and print posts, publish nothing")
    args = parser.parse_args()
    cfg = Config()
    if args.dry_run:
        cfg.dry_run = True
    return run(cfg)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        # requests exceptions can embed URLs; the Telegram token only appears in publishers.py,
        # which redacts it, but we still avoid dumping full tracebacks of HTTP objects.
        log.error("Unexpected error: %s: %s", type(exc).__name__, exc)
        raise
