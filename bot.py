import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime
import random

# ====================== CONFIG ======================
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"
BINANCE_SQUARE_API_KEY = "YOUR_BINANCE_SQUARE_OPENAPI_KEY"

MIN_SCORE = 50
SCAN_INTERVAL = 90          # seconds
COOLDOWN_MINUTES = 45       # same coin cooldown
MIN_24H_VOLUME = 800000     # USDT

# ====================================================

BASE_URL = "https://api.binance.com"
SQUARE_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"

headers_square = {
    "X-Square-OpenAPI-Key": BINANCE_SQUARE_API_KEY,
    "Content-Type": "application/json"
}

alerted = {}  # symbol: last_alert_time

def get_all_usdt_symbols():
    res = requests.get(f"{BASE_URL}/api/v3/exchangeInfo").json()
    symbols = []
    for s in res["symbols"]:
        if (s["quoteAsset"] == "USDT" and 
            s["status"] == "TRADING" and 
            s["isSpotTradingAllowed"] and
            not any(x in s["baseAsset"] for x in ["UP", "DOWN", "BULL", "BEAR"])):
            symbols.append(s["symbol"])
    return symbols

def get_klines(symbol, interval, limit=100):
    url = f"{BASE_URL}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = requests.get(url, params=params).json()
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
        df1h = get_klines(symbol, "1h", 80)
        df4h = get_klines(symbol, "4h", 80)

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
        print(f"Error {symbol}: {e}")
        return 0, [], {}

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

def generate_square_post(symbol, price, score, change):
    templates = [
        f"{symbol} looking strong right now.\nPrice: {price:.6f}\nScore: {score}/100\nMomentum building on Spot.",
        f"Interesting move on {symbol}.\n+{change:.2f}% recently with solid volume.\nWatching closely.",
        f"{symbol} showing clean strength.\nBuyers stepping in.\nScore hit {score}.",
        f"Volume picking up on {symbol}.\nPrice action looks constructive.\nBinance Spot.",
        f"{symbol} breaking out with conviction.\nCurrent price {price:.6f}\nKeeping an eye on this one.",
        f"Strong order flow detected on {symbol}.\nScore: {score}\nWorth monitoring.",
        f"{symbol} heating up on the 15m-1h structure.\nVolume confirmation present.",
        f"Clean setup forming on {symbol}.\nPrice: {price:.6f} | Score {score}"
    ]
    return random.choice(templates)

def post_to_square(text):
    payload = {
        "bodyTextOnly": text,
        "onlyText": True
    }
    try:
        res = requests.post(SQUARE_URL, json=payload, headers=headers_square, timeout=10)
        print("Square post status:", res.status_code)
        return res.status_code == 200
    except Exception as e:
        print("Square error:", e)
        return False

def main():
    print("Bot started...")
    symbols = get_all_usdt_symbols()
    print(f"Total symbols: {len(symbols)}")

    while True:
        try:
            # Get 24h volume filter
            tickers = requests.get(f"{BASE_URL}/api/v3/ticker/24hr").json()
            vol_map = {t["symbol"]: float(t["quoteVolume"]) for t in tickers}

            candidates = []
            for sym in symbols:
                if vol_map.get(sym, 0) < MIN_24H_VOLUME:
                    continue
                if sym in alerted and (time.time() - alerted[sym]) < COOLDOWN_MINUTES * 60:
                    continue

                score, details, info = calculate_score(sym)
                if score >= MIN_SCORE:
                    candidates.append((sym, score, details, info))

            # Sort by score
            candidates.sort(key=lambda x: x[1], reverse=True)

            for sym, score, details, info in candidates[:4]:  # max 4 per cycle
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
Time: {datetime.now().strftime('%H:%M:%S')}
"""
                send_telegram(msg)
                print(f"Alert sent: {sym} | Score {score}")

                # Square post
                post_text = generate_square_post(sym, info['price'], score, info['change_15m'])
                post_to_square(post_text)
                time.sleep(random.randint(11, 16))  # 10-15s gap

                alerted[sym] = time.time()

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            print("Main loop error:", e)
            time.sleep(30)

if __name__ == "__main__":
    main()
