import os
import sys
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import numpy as np
from datetime import datetime
import random

# ====================== CONFIG ======================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BINANCE_SQUARE_API_KEY = os.getenv("BINANCE_SQUARE_API_KEY", "").strip()

MIN_SCORE = 50
SCAN_INTERVAL = 90          # seconds
COOLDOWN_MINUTES = 45       # same coin cooldown
MIN_24H_VOLUME = 800000     # USDT

# ====================================================

BASE_URLS = ["https://data-api.binance.vision", "https://api.binance.com"]
STATE_FILE = os.getenv("BOT_STATE_FILE", "state.json")
MAX_RUNTIME = 8 * 60       # seconds; GitHub job is killed at 10 min
SESSION = requests.Session()


def binance_get(endpoint, params=None, tries=3):
    """GET with fallback URLs, status check and backoff. Raises RuntimeError on failure."""
    last = "unknown"
    for attempt in range(tries):
        for base in BASE_URLS:
            try:
                r = SESSION.get(base + endpoint, params=params, timeout=12)
            except requests.RequestException as e:
                last = f"{base}: network error {type(e).__name__}"
                continue
            if r.status_code == 200:
                return r.json()
            last = f"{base}: HTTP {r.status_code}"
            if r.status_code in (418, 429):
                time.sleep(min(2 ** attempt * 2, 20))
        time.sleep(1 + attempt)
    raise RuntimeError(f"Binance request failed ({endpoint}): {last}")

SQUARE_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"

headers_square = {
    "X-Square-OpenAPI-Key": BINANCE_SQUARE_API_KEY,
    "Content-Type": "application/json",
    "clienttype": "binanceSkill"
}

alerted = {}  # symbol: last_alert_time

def get_all_usdt_symbols():
    res = binance_get("/api/v3/exchangeInfo")
    symbols = []
    for s in res["symbols"]:
        if (s["quoteAsset"] == "USDT" and 
            s["status"] == "TRADING" and 
            s["isSpotTradingAllowed"] and
            not any(x in s["baseAsset"] for x in ["UP", "DOWN", "BULL", "BEAR"])):
            symbols.append(s["symbol"])
    return symbols

def get_klines(symbol, interval, limit=100):
    data = binance_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    df = df.astype(float)
    return df

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def sma(series, period):
    return series.rolling(period).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def bollinger(series, period=20):
    mid = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = mid + 2 * std
    lower = mid - 2 * std
    return upper, mid, lower

def calculate_score(symbol):
    try:
        df15 = get_klines(symbol, "15m", 80)
        df1h = get_klines(symbol, "1h", 150)
        df4h = get_klines(symbol, "4h", 100)

        score = 0
        details = []

        # ========== 15m (10 points) ==========
        close15 = df15["close"]
        ema10_15 = ema(close15, 10)
        ema20_15 = ema(close15, 20)
        ema55_15 = ema(close15, 55)
        rsi15 = rsi(close15).iloc[-1]
        upper15, mid15, lower15 = bollinger(close15)

        s15 = 0
        if close15.iloc[-1] > ema10_15.iloc[-1] > ema20_15.iloc[-1] > ema55_15.iloc[-1]:
            s15 += 4
        if close15.iloc[-1] > upper15.iloc[-1] and (upper15.iloc[-1] - lower15.iloc[-1]) > (upper15.iloc[-5] - lower15.iloc[-5]):
            s15 += 3
        if 55 <= rsi15 <= 72:
            s15 += 3
        score += min(s15, 10)
        details.append(f"15m: {min(s15,10)}")

        # ========== 1h (25 points) ==========
        close1h = df1h["close"]
        ema10_1h = ema(close1h, 10)
        ema20_1h = ema(close1h, 20)
        ema55_1h = ema(close1h, 55)
        ma50_1h = sma(close1h, 50)
        ma100_1h = sma(close1h, 100)
        rsi1h = rsi(close1h).iloc[-1]

        s1h = 0
        if close1h.iloc[-1] > ema10_1h.iloc[-1] > ema20_1h.iloc[-1] > ema55_1h.iloc[-1]:
            s1h += 9
        if close1h.iloc[-1] > ma50_1h.iloc[-1] and close1h.iloc[-1] > ma100_1h.iloc[-1]:
            s1h += 8
        if 52 <= rsi1h <= 70:
            s1h += 8
        score += min(s1h, 25)
        details.append(f"1h: {min(s1h,25)}")

        # ========== 4h (20 points) ==========
        close4h = df4h["close"]
        ema20_4h = ema(close4h, 20)
        ema55_4h = ema(close4h, 55)
        ma50_4h = sma(close4h, 50)

        s4h = 0
        if close4h.iloc[-1] > ema20_4h.iloc[-1] and close4h.iloc[-1] > ema55_4h.iloc[-1]:
            s4h += 12
        if close4h.iloc[-1] > ma50_4h.iloc[-1]:
            s4h += 8
        score += min(s4h, 20)
        details.append(f"4h: {min(s4h,20)}")

        # ========== Volume + Order Flow (30 points) ==========
        vol = df15["volume"]
        taker_buy = df15["taker_buy_base"]
        avg_vol = vol.iloc[-15:-1].mean()
        current_vol = vol.iloc[-1]
        vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0

        taker_ratio = taker_buy.iloc[-1] / vol.iloc[-1] if vol.iloc[-1] > 0 else 0
        # Simple Volume Delta approximation
        delta = (taker_buy.iloc[-3:].sum() - (vol.iloc[-3:].sum() - taker_buy.iloc[-3:].sum()))

        s_vol = 0
        if vol_ratio >= 3.0:
            s_vol += 14
        elif vol_ratio >= 2.5:
            s_vol += 10
        elif vol_ratio >= 2.0:
            s_vol += 6

        if taker_ratio >= 0.62:
            s_vol += 10
        elif taker_ratio >= 0.55:
            s_vol += 6

        if delta > 0:
            s_vol += 6

        score += min(s_vol, 30)
        details.append(f"Vol+OF: {min(s_vol,30)}")

        # ========== Extra Confluence (15 points) ==========
        s_extra = 0
        # Simple higher high check
        if close15.iloc[-1] > close15.iloc[-4] and close1h.iloc[-1] > close1h.iloc[-3]:
            s_extra += 6
        # RSI alignment bonus
        if rsi15 > 55 and rsi1h > 52:
            s_extra += 5
        # Price above key MAs multi TF
        if close15.iloc[-1] > ema20_15.iloc[-1] and close1h.iloc[-1] > ma50_1h.iloc[-1]:
            s_extra += 4

        score += min(s_extra, 15)
        details.append(f"Extra: {min(s_extra,15)}")

        return round(score, 1), details, {
            "price": close15.iloc[-1],
            "change_15m": ((close15.iloc[-1] - close15.iloc[-2]) / close15.iloc[-2]) * 100,
            "vol_ratio": round(vol_ratio, 2),
            "taker_ratio": round(taker_ratio * 100, 1),
            "rsi_15m": round(rsi15, 1)
        }

    except Exception as e:
        return 0, [], {"error": str(e)}

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Telegram skipped: secrets not set")
        return
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print("Telegram FAILED: HTTP", r.status_code, r.text[:200].replace(TELEGRAM_BOT_TOKEN, "***"))
    except Exception as e:
        print("Telegram error:", type(e).__name__)

# All hashtags in one pool. Every Square post gets exactly TAGS_PER_POST of them, and the picks are random,
# so consecutive posts never share a tag. (The trailing space after the fire emoji keeps the
# "#SPOTCALL\U0001F525\U0001F525\U0001F525  #next" double-space look.)
ALL_TAGS = ["#spottrading", "#SPOTCALL\U0001F525\U0001F525\U0001F525 ", "#signal", "#Spot",
            "#SpotTradingSuccess", "#TradingSignal", "#TradingTips", "#BullishMomentum", "#SignalAlert"]
TAGS_PER_POST = 2


def next_post_tags(state, rng=random):
    """Pick TAGS_PER_POST random tags, never reusing a tag from the previous post.

    The previous post's tags are remembered in state.json ("_last_tags"), so this also holds across runs.
    """
    last = set(state.get("_last_tags", []))
    pool = [t for t in ALL_TAGS if t not in last] or list(ALL_TAGS)
    picks = rng.sample(pool, TAGS_PER_POST)
    state["_last_tags"] = picks
    return picks


def generate_square_post(symbol, price, score, change, post_tags=None):
    coin = symbol.replace("USDT", "")
    tags = "\n" + " ".join(post_tags or ALL_TAGS[:TAGS_PER_POST]).strip()
    templates = [
        f"${coin} looking strong right now.\nPrice: {price:.6f}\nMomentum building on Spot.",
        f"${coin} showing clean strength.\nBuyers stepping in.",
        f"Volume picking up on ${coin}.\nPrice action looks constructive.\nBinance Spot.",
        f"${coin} breaking out with conviction.\nCurrent price {price:.6f}\nKeeping an eye on this one.",
        f"Strong order flow detected on ${coin}.\nWorth monitoring.",
        f"${coin} heating up on the 15m-1h structure.\nVolume confirmation present.",
        f"Clean setup forming on ${coin}.\nPrice: {price:.6f}",
    ]
    if change >= 0.3:  # only claim a move when the 15m change is really positive
        templates.append(f"Interesting move on ${coin}.\n{change:+.2f}% recently with solid volume.\nWatching closely.")
    return random.choice(templates) + tags


def post_to_square(text):
    if not BINANCE_SQUARE_API_KEY:
        print("Square skipped: BINANCE_SQUARE_API_KEY not set")
        return False
    try:
        res = requests.post(SQUARE_URL, json={"bodyTextOnly": text}, headers=headers_square, timeout=15)
        body = res.json() if res.content else {}
        ok = res.status_code == 200 and str(body.get("code")) == "000000"
        print("Square post:", "OK" if ok else f"FAILED http={res.status_code} code={body.get('code')} {body.get('message','')}")
        return ok
    except Exception as e:
        print("Square error:", type(e).__name__)
        return False

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


def main():
    start = time.time()
    print("Bot started (single run)...")
    alerted = load_state()
    now = time.time()
    alerted = {k: v for k, v in alerted.items()
               if k.startswith("_") or now - v < COOLDOWN_MINUTES * 60}
    try:
        symbols = get_all_usdt_symbols()
        tickers = binance_get("/api/v3/ticker/24hr")
    except RuntimeError as e:
        print("FATAL:", e)
        return 1
    vol_map = {t["symbol"]: float(t["quoteVolume"]) for t in tickers}
    todo = [s_ for s_ in symbols if vol_map.get(s_, 0) >= MIN_24H_VOLUME and s_ not in alerted]
    print(f"Total symbols: {len(symbols)} | to scan: {len(todo)}")

    candidates, errors = [], 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(calculate_score, s_): s_ for s_ in todo}
        for fut in as_completed(futs):
            score, details, info = fut.result()
            if "error" in info:
                errors += 1
            elif score >= MIN_SCORE:
                candidates.append((futs[fut], score, details, info))
    print(f"Scan done in {time.time()-start:.0f}s | candidates: {len(candidates)} | errors: {errors}")

    candidates.sort(key=lambda x: x[1], reverse=True)
    for i, (sym, score, details, info) in enumerate(candidates[:4]):
        if time.time() - start > MAX_RUNTIME:
            print("Runtime budget reached, stopping")
            break
        if i:
            time.sleep(random.randint(10, 15))
        msg = f"""
<b>Binance Spot Signal</b>

Symbol: <b>{sym}</b>
Score: <b>{score}/100</b>
Price: {info['price']:.6f}
15m Change: {info['change_15m']:.2f}%
Vol Ratio: {info['vol_ratio']}x
Taker Buy: {info['taker_ratio']}%
RSI 15m: {info['rsi_15m']}

Details: {' | '.join(details)}
Time: {datetime.utcnow().strftime('%H:%M:%S')} UTC
"""
        send_telegram(msg)
        print(f"Alert sent: {sym} | Score {score}")
        post_to_square(generate_square_post(sym, info['price'], score, info['change_15m'],
                                            next_post_tags(alerted)))
        alerted[sym] = time.time()
        save_state(alerted)
    save_state(alerted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
