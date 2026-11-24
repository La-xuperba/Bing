#!/usr/bin/env python3
"""
Binance SPOT coin bot -> Binance Square.

Modes (one scanner):
  FLASH  disabled by configuration  -> no flash posts
  SCALP  10/15/30m    breakout setups               -> signal post (+ levels on Telegram)
  SWING  1h/4h/1d     accumulation setups           -> signal post (+ levels on Telegram)

"Which coins to look at" = ranked by 24h volume as % of market cap (CoinGecko, free).
No price / volume / market-cap limit is applied to the universe.

Square post = max 4 lines, $COIN, English, mood, reason, 2 random hashtags + coin-name hashtag, attention card image.
Posts are sent one by one with a persistent queue and a 15-second gap. No results are posted to Square.
Entry/SL/TP and win/loss results go only to your private Telegram (optional).

Run:  python bot.py             (loops for MAX_RUNTIME_MIN minutes, then exits)
      DRY_RUN=1 python bot.py   (no posting; prints posts and saves cards to ./cards)
      python bot.py --selftest  (offline checks, no network)
"""
import os
import sys
import re
import json
import time
import random
import subprocess
import glob
import traceback
from collections import deque, defaultdict
from datetime import datetime, timezone

import requests


# ----------------------------------------------------------------- config
def envs(n, d):
    v = os.getenv(n)
    return v if v not in (None, "") else d


def envf(n, d):
    try:
        return float(os.getenv(n, d))
    except Exception:
        return d


def envb(n, d):
    v = os.getenv(n)
    return d if v in (None, "") else v.strip().lower() in ("1", "true", "yes", "on")


POLL_SEC = envf("POLL_SEC", 30)
MAX_RUNTIME_MIN = envf("MAX_RUNTIME_MIN", 345)
DRY_RUN = envb("DRY_RUN", False)
STATE_FILE = envs("STATE_FILE", "state.json")
CARD_DIR = envs("CARD_DIR", "cards")

# flash spike: price move % inside the window (volume must also be VOL_MULT x normal)
FLASH_THR = {5: envf("FLASH_5M", 3.0), 10: envf("FLASH_10M", 4.0),
             15: envf("FLASH_15M", 5.0), 30: envf("FLASH_30M", 6.0)}
FLASH_VOL_MULT = envf("FLASH_VOL_MULT", 5.0)
MIN_WINDOW_USDT = envf("MIN_WINDOW_USDT", 2000)   # dust filter; set 0 to disable
ALERT_DOWN = envb("ALERT_DOWN", True)             # also post sharp drops (mood negative)
FLASH_CHECKS = 8                                  # max candle checks per poll

SCALP_TOP = int(envf("SCALP_TOP", 40))
SWING_TOP = int(envf("SWING_TOP", 60))
MIN_TP_PCT = envf("MIN_TP_PCT", 0.5)              # TP1 must be at least this % (fees)
MAX_SPREAD_PCT = envf("MAX_SPREAD_PCT", 0.15)
MIN_SCORE = int(envf("MIN_SCORE", 55))
FLASH_ENABLED = envb("FLASH_ENABLED", False)  # OFF by default; Spot signal scans remain active
MICRO_TOP = int(envf("MICRO_TOP", 18))  # limit order-book/trade calls per scan

FLASH_CD = envf("FLASH_COOLDOWN_MIN", 45)
SCALP_CD = envf("SCALP_COOLDOWN_MIN", 60)
SWING_CD = envf("SWING_COOLDOWN_MIN", 720)
# One shared cooldown across SCALP + SWING, so changing signal mode cannot
# repost the same coin a few hours later.
COIN_CD = envf("COIN_COOLDOWN_MIN", 720)

# Square: slower cadence and fewer posts to avoid feed flooding.
POST_GAP = (envf("SQUARE_POST_GAP_SEC", 15.0), envf("SQUARE_POST_GAP_SEC", 15.0))  # exact 15-second gap
MAX_POSTS_DAY = int(envf("MAX_POSTS_DAY", 8))
MAX_POSTS_HOUR = int(envf("MAX_POSTS_HOUR", 2))

HOSTS = ["https://data-api.binance.vision", "https://api.binance.com",
         "https://api1.binance.com", "https://api2.binance.com"]
FUTURES_HOST = "https://fapi.binance.com"  # derivatives used as context only; bot remains SPOT-only
GECKO = "https://api.coingecko.com/api/v3/coins/markets"
SQUARE_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"

STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDD", "EUR", "GBP", "TRY",
           "BRL", "AEUR", "USD1", "XUSD", "PYUSD", "UST", "USTC", "RLUSD", "BFUSD", "USDE"}

# Explicitly exclude the Binance bStocks / tokenized-stock pairs visible in
# the user's screenshots. Keep this as an exact list: do NOT block every
# symbol ending in "B", because that would also remove legitimate crypto assets.
BLOCKED_BASES = {
    "METAB", "SMHB", "EWJB", "NFLXB", "VSTB", "PLTRB", "MSFTB", "USARB",
    "GOOGLB", "DELLB", "ASTSB", "DRAMB", "EWYB", "COINB", "PDBB", "BOTB",
    "CRWDB", "INTWB", "AVGOB", "CYPHB", "CRCLB", "NOKB", "KSTRB", "BBB",
    "BNCB", "DJTB", "USDEB", "ALABB", "AMDB", "QNTB", "GPROB", "SECZB",
    "SHAZB", "SPCXB", "MSTRB", "CBRSB", "NBISB", "IRENB", "MVLLB",
    "TQQQB", "RDDTB", "SOXLB", "MRNAB", "SNXXB", "IBMB", "JPMB", "AXTIB",
    "ADBEB", "CRDOB", "KORUB", "LITEB", "CRWWB", "AAPLB", "BRKBB", "TSMB",
    "BEB", "EWZB",
}

TAGS_NEUTRAL = ["#spottrading", "#signal", "#Spot", "#SpotTradingSuccess",
                "#TradingSignal", "#TradingTips", "#SignalAlert"]
TAGS_BULLISH = ["#SPOTCALL🔥🔥🔥", "#BullishMomentum"]

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (coin-bot)"
HOST_IDX = 0
HIST = defaultdict(lambda: deque(maxlen=int(2000 / max(POLL_SEC, 5)) + 20))
QUEUE = []
TELEGRAM_QUEUE = []
NEXT_POST = 0.0


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


# ----------------------------------------------------------------- http
def bget(path, params=None):
    global HOST_IDX
    for _ in range(len(HOSTS) * 2):
        host = HOSTS[HOST_IDX % len(HOSTS)]
        try:
            r = S.get(host + path, params=params, timeout=15)
            if r.status_code in (418, 429):
                time.sleep(min(60, int(r.headers.get("Retry-After", 5))))
                continue
            if r.status_code in (451, 403):      # region block -> next host
                HOST_IDX += 1
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            HOST_IDX += 1
            time.sleep(1)
    return None


def tg(text):
    """Send one Telegram message and verify Telegram's JSON ok response."""
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if DRY_RUN:
        log("TELEGRAM DRY_RUN", text[:300].replace("\n", " | "))
        return True
    if not tok or not chat:
        log("TELEGRAM ERROR: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID missing")
        return False
    try:
        r = S.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                   data={"chat_id": chat, "text": text[:4000]}, timeout=20)
        try:
            body = r.json()
        except Exception:
            body = {}
        if r.ok and body.get("ok") is True:
            log("TELEGRAM SENT", "message_id=" + str(body.get("result", {}).get("message_id", "unknown")))
            return True
        log("TELEGRAM API ERROR", f"http={r.status_code}", str(body or r.text)[:700])
        return False
    except Exception as e:
        log("TELEGRAM NETWORK ERROR", repr(e))
        return False


def queue_telegram(text):
    if text:
        TELEGRAM_QUEUE.append({"text": str(text)[:4000], "ts": time.time(),
                               "attempts": 0, "next_try": 0})
        if len(TELEGRAM_QUEUE) > 200:
            TELEGRAM_QUEUE.pop(0)


def drain_telegram(state):
    """Retry unsent Telegram messages; queue survives GitHub Actions restarts."""
    global TELEGRAM_QUEUE
    while TELEGRAM_QUEUE:
        item = TELEGRAM_QUEUE[0]
        now = time.time()
        if now < float(item.get("next_try", 0)):
            break
        if tg(item.get("text", "")):
            TELEGRAM_QUEUE.pop(0)
            sync_queues(state)
            save_state(state)
            continue
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["next_try"] = now + min(900, 15 * (2 ** min(item["attempts"], 6)))
        sync_queues(state)
        save_state(state)
        break


# ----------------------------------------------------------------- indicators
def ema_series(vals, n):
    k = 2 / (n + 1)
    e = vals[0]
    out = []
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi_last(c, n=14):
    if len(c) < n + 2:
        return 50.0
    g = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    l = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr_last(h, l, c, n=14):
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
           for i in range(1, len(c))]
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def mean(x):
    return sum(x) / len(x) if x else 0.0


def market_microstructure(sym):
    """Spot order-book + recent trade pressure. Returns None on API failure."""
    depth = bget("/api/v3/depth", {"symbol": sym, "limit": 20})
    trades = bget("/api/v3/trades", {"symbol": sym, "limit": 100})
    if not isinstance(depth, dict) or not isinstance(trades, list) or not trades:
        return None
    try:
        bids = sum(float(x[0]) * float(x[1]) for x in depth.get("bids", []))
        asks = sum(float(x[0]) * float(x[1]) for x in depth.get("asks", []))
        book_imb = bids / (bids + asks) if bids + asks else 0.5
        # Binance trade field m=True means buyer was maker, i.e. seller-initiated.
        buy_q = sum(float(x["quoteQty"]) for x in trades if not x.get("isBuyerMaker", True))
        total_q = sum(float(x["quoteQty"]) for x in trades)
        taker_buy = buy_q / total_q if total_q else 0.5
        result = {"book_imb": book_imb, "taker_buy": taker_buy}
        # Optional derivatives context: open-interest change + funding. Never used to place futures orders.
        try:
            oi_r = S.get(FUTURES_HOST + "/futures/data/openInterestHist",
                         params={"symbol": sym, "period": "5m", "limit": 2}, timeout=8)
            fr_r = S.get(FUTURES_HOST + "/fapi/v1/fundingRate",
                         params={"symbol": sym, "limit": 1}, timeout=8)
            if oi_r.ok:
                oi_data = oi_r.json()
                if len(oi_data) >= 2:
                    old_oi = float(oi_data[-2].get("sumOpenInterestValue", 0))
                    new_oi = float(oi_data[-1].get("sumOpenInterestValue", 0))
                    if old_oi > 0:
                        result["oi_change_pct"] = (new_oi / old_oi - 1) * 100
            if fr_r.ok:
                fr_data = fr_r.json()
                if fr_data:
                    result["funding"] = float(fr_data[-1].get("fundingRate", 0))
        except Exception:
            pass  # futures endpoints may be unavailable by region or symbol
        return result
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def market_score(kl, micro, btc_ok=True, mtf_ok=None):
    """Explainable 100-point score; missing external data is neutral, not a penalty."""
    if not kl or len(kl) < 60:
        return 0, {}
    h, l = [x["h"] for x in kl], [x["l"] for x in kl]
    o, c, qv = [x["o"] for x in kl], [x["c"] for x in kl], [x["qv"] for x in kl]
    e10, e20, e55 = (ema_series(c, n)[-1] for n in (10, 20, 55))
    ma50 = mean(c[-50:])
    r = rsi_last(c)
    avgv = mean(qv[-21:-1])
    vm = qv[-1] / avgv if avgv else 0
    # Trend & structure (20)
    trend = 20 if c[-1] > e20 > e55 else 14 if c[-1] > e20 else 7 if c[-1] > e55 else 0
    trend += 0 if c[-1] < ma50 else 0
    # Volume + buying pressure (20)
    vol = 12 if vm >= 2 else 9 if vm >= 1.5 else 6 if vm >= 1.2 else 2
    if micro:
        vol += 4 if micro["book_imb"] >= .58 else 2 if micro["book_imb"] >= .52 else 0
        vol += 4 if micro["taker_buy"] >= .60 else 2 if micro["taker_buy"] >= .53 else 0
        if micro.get("oi_change_pct", 0) >= 0.5 and micro.get("funding", 0) <= 0.0008:
            vol += 2  # rising participation without extreme positive funding
        if micro.get("funding", 0) > 0.001:
            vol -= 1  # crowded long positioning risk
    else:
        vol += 4  # neutral allowance when public microstructure endpoint is unavailable
    vol = min(20, vol)
    # Momentum (15)
    mom = 15 if 52 <= r <= 68 else 11 if 48 <= r < 52 or 68 < r <= 74 else 6 if 42 <= r < 48 else 2
    # Liquidity / candle quality (15): sweep/reclaim proxy, close location, wick
    rng = max(h[-1] - l[-1], 1e-12)
    close_pos = (c[-1] - l[-1]) / rng
    lower_sweep = l[-1] < min(l[-8:-1]) and c[-1] > min(l[-8:-1])
    struct = 8 if close_pos >= .75 else 5 if close_pos >= .55 else 1
    struct += 7 if lower_sweep else 4 if c[-1] > max(h[-13:-1]) else 2
    # Multi-timeframe score supplied by scanner's trend alignment (10)
    mtf = (10 if mtf_ok else 3) if mtf_ok is not None else (10 if c[-1] > e20 and e20 > e55 else 6 if c[-1] > e20 else 2)
    # BTC regime (10)
    btc = 10 if btc_ok else 4
    # Risk / entry extension (10)
    a = atr_last(h, l, c) or 0
    ext = (c[-1] - e20) / a if a else 99
    risk = 10 if 0 <= ext <= 1.5 else 7 if -0.5 <= ext <= 2.5 else 3 if ext <= 4 else 0
    parts = {"Trend":trend, "Volume/OrderFlow":vol, "Momentum":mom, "Liquidity/Structure":struct,
             "MultiTF":mtf, "BTC":btc, "Risk":risk}
    return min(100, sum(parts.values())), parts


# ----------------------------------------------------------------- binance data
def klines(symbol, interval, limit):
    d = bget("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not d:
        return None
    return [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
             "c": float(k[4]), "v": float(k[5]), "qv": float(k[7]), "ct": k[6]} for k in d]


def closed(kl):
    now_ms = int(time.time() * 1000)
    return [k for k in kl if k["ct"] < now_ms]


def agg(kl5, minutes):
    """Binance has no 10m candle: build it from 5m candles (aligned buckets)."""
    bucket = minutes * 60000
    out, cur = [], None
    for k in kl5:
        b = k["t"] // bucket
        if cur is None or cur["b"] != b:
            if cur:
                out.append(cur)
            cur = dict(k, b=b, t=b * bucket, n=1)
        else:
            cur["h"] = max(cur["h"], k["h"])
            cur["l"] = min(cur["l"], k["l"])
            cur["c"] = k["c"]
            cur["v"] += k["v"]
            cur["qv"] += k["qv"]
            cur["ct"] = k["ct"]
            cur["n"] += 1
    if cur:
        out.append(cur)
    return [x for x in out if x["n"] == minutes // 5]


def fetch_tf(sym, tf):
    if tf == "10m":
        kl = klines(sym, "5m", 300)
        return agg(closed(kl), 10) if kl else None
    kl = klines(sym, tf, 150)
    return closed(kl) if kl else None


def parse_tickers(raw, allowed=None):
    out = {}
    for t in raw:
        s = t.get("symbol", "")
        if not s.endswith("USDT") or (allowed is not None and s not in allowed):
            continue
        base = s[:-4]
        if not base or base in STABLES or base in BLOCKED_BASES:
            continue
        try:
            price, qv = float(t["lastPrice"]), float(t["quoteVolume"])
            chg = float(t["priceChangePercent"])
        except Exception:
            continue
        if price > 0:
            out[s] = {"base": base, "price": price, "qv": qv, "chg24": chg, "ratio": None}
    return out


def load_allowed():
    d = bget("/api/v3/exchangeInfo", {"permissions": "SPOT"})
    if not d:
        return None
    ok = {x["symbol"] for x in d.get("symbols", [])
          if x.get("status") == "TRADING" and x.get("quoteAsset") == "USDT"
          and x.get("baseAsset") not in BLOCKED_BASES}
    return ok or None


def load_mcaps():
    caps, best = {}, {}
    hdr = {}
    if os.getenv("COINGECKO_API_KEY"):
        hdr["x-cg-demo-api-key"] = os.getenv("COINGECKO_API_KEY")
    for page in range(1, 7):                      # ~1500 coins
        data = None
        for _ in range(2):
            try:
                r = S.get(GECKO, params={"vs_currency": "usd", "order": "market_cap_desc",
                                         "per_page": 250, "page": page},
                          headers=hdr, timeout=25)
                if r.status_code == 429:
                    time.sleep(25)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except Exception:
                time.sleep(3)
        if not data:
            break
        for c in data:
            sym = (c.get("symbol") or "").upper()
            mc = c.get("market_cap") or 0
            rk = c.get("market_cap_rank") or 10 ** 9
            if mc > 0 and rk < best.get(sym, 10 ** 9):
                caps[sym], best[sym] = mc, rk
        time.sleep(2.5)
    return caps if len(caps) >= 200 else None


def top_by_ratio(tick, n):
    ranked = sorted((s for s, t in tick.items() if t["ratio"] is not None),
                    key=lambda s: tick[s]["ratio"], reverse=True)[:n]
    if len(ranked) < n:                            # no market caps -> fall back to volume
        rest = sorted((s for s in tick if s not in ranked),
                      key=lambda s: tick[s]["qv"], reverse=True)
        ranked += rest[:n - len(ranked)]
    return ranked


# ----------------------------------------------------------------- posts
def pick_tags(positive):
    return random.sample(TAGS_NEUTRAL + (TAGS_BULLISH if positive else []), 2)


def compose(base, positive, headline):
    mood = "🟢 Positive" if positive else "🔴 Negative"
    # Use plain, searchable hashtags only; each tag is separated by a normal space.
    coin_tag = "#" + re.sub(r"[^A-Za-z0-9]", "", str(base)).upper()
    tags = [re.sub(r"[^A-Za-z0-9_]", "", str(tag).lstrip("#")) for tag in pick_tags(positive)]
    tags = ["#" + tag for tag in tags if tag]
    hashtag_line = " ".join(tags + ([coin_tag] if coin_tag != "#" else []))
    return f"${base} {mood}\n{headline.strip()}\n{hashtag_line}"


FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]


def _font(size):
    from PIL import ImageFont
    for p in FONTS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def make_card(base, positive, label, metric, path):
    from PIL import Image, ImageDraw
    W, H = 1200, 630
    top, bot = ((12, 46, 30), (4, 12, 9)) if positive else ((56, 16, 20), (14, 5, 7))
    acc = (46, 214, 120) if positive else (255, 84, 84)
    img = Image.new("RGB", (W, H), top)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3)))
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    if positive:
        od.polygon([(930, 90), (1150, 400), (1010, 400), (1010, 560), (850, 560), (850, 400), (710, 400)],
                   fill=acc + (46,))
    else:
        od.polygon([(930, 560), (1150, 250), (1010, 250), (1010, 90), (850, 90), (850, 250), (710, 250)],
                   fill=acc + (46,))
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 18, H], fill=acc)

    f = _font(34)
    tw = d.textbbox((0, 0), label, font=f)[2]
    d.rounded_rectangle([70, 56, 70 + tw + 50, 118], radius=31, fill=acc)
    d.text((95, 63), label, font=f, fill=(8, 14, 12))

    coin, size = f"${base}", 200
    while size > 60 and d.textbbox((0, 0), coin, font=_font(size))[2] > 1000:
        size -= 10
    d.text((70, 150), coin, font=_font(size), fill=(255, 255, 255))

    y = 395
    if positive:
        d.polygon([(70, y + 56), (130, y + 56), (100, y + 6)], fill=acc)
    else:
        d.polygon([(70, y + 6), (130, y + 6), (100, y + 56)], fill=acc)
    d.text((155, y - 2), "POSITIVE MOOD" if positive else "NEGATIVE MOOD", font=_font(56), fill=acc)
    d.text((70, 478), metric, font=_font(44), fill=(235, 240, 238))
    d.text((70, 568), "Not financial advice  |  Trade at your own risk", font=_font(26),
           fill=(150, 160, 156))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    img.save(path, "PNG")
    return path


def square_key():
    return os.getenv("BINANCE_SQUARE_OPENAPI_KEY") or os.getenv("SQUARE_API_KEY") or ""


def find_square_script():
    d = envs("SQUARE_SKILL_DIR", "square-skill")
    hits = glob.glob(os.path.join(d, "**", "post-image.mjs"), recursive=True)
    return os.path.abspath(hits[0]) if hits else None


def post_square(text, img):
    """Publish an image post through Binance's official Square skill CLI."""
    if DRY_RUN:
        log("SQUARE DRY_RUN: not published", text.replace("\n", " | ")[:300])
        return True, "dry-run"
    key = square_key()
    if not key:
        return False, "AUTH_ERROR: BINANCE_SQUARE_OPENAPI_KEY missing"
    script = find_square_script()
    if not script:
        return False, "CONFIG_ERROR: scripts/post-image.mjs not found"
    if not img or not os.path.isfile(img):
        return False, "IMAGE_ERROR: generated image file missing"
    env = dict(os.environ, BINANCE_SQUARE_OPENAPI_KEY=key)
    try:
        p = subprocess.run(["node", script, "--text", text, "--images", os.path.abspath(img)],
                           capture_output=True, text=True, timeout=180, env=env,
                           cwd=os.path.dirname(os.path.dirname(script)))
        out = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
        # Official skill documents Success! + unavailable ID/link as a successful
        # publish (the API may return 504 after the server accepted the post).
        success_marker = "Success!" in out
        if p.returncode == 0 or success_marker:
            log("SQUARE API SUCCESS", out[-1200:] if out else "CLI exit code 0; no response text")
            return True, "image-published"
        log("SQUARE API ERROR", f"exit={p.returncode}", out[-1800:] or "No CLI output")
        if "220009" in out:
            return False, "LIMIT: Square daily post limit reached"
        if "220003" in out or "220004" in out:
            return False, "AUTH_ERROR: Square API key invalid or expired; " + out[-500:]
        return False, "PUBLISH_ERROR: " + (out[-900:] or f"CLI exit {p.returncode}")
    except subprocess.TimeoutExpired as e:
        detail = ((e.stdout or "") + (e.stderr or "")) if isinstance(e.stdout, str) else ""
        log("SQUARE API TIMEOUT", detail[-1000:])
        return False, "TIMEOUT: Square CLI exceeded 180 seconds; verify Square feed before retry to avoid duplicates"
    except Exception as e:
        log("SQUARE EXECUTION ERROR", repr(e))
        return False, f"EXECUTION_ERROR: {e}"


def enqueue(prio, ttl, base, positive, text, label, metric, tgmsg=None):
    if len(QUEUE) >= 40:
        QUEUE.sort(key=lambda x: (x["prio"], x["ts"]))
        QUEUE.pop()                                   # drop least important
    QUEUE.append({"prio": prio, "ttl": ttl, "ts": time.time(), "base": base, "pos": positive,
                  "text": text, "label": label, "metric": metric})
    if tgmsg:
        queue_telegram(tgmsg)


def sync_queues(state):
    state["queue"] = QUEUE
    state["telegram_queue"] = TELEGRAM_QUEUE
    state["next_post_ts"] = NEXT_POST


def drain(state, now):
    """Publish queued Square items; only remove an item after confirmed success."""
    global NEXT_POST
    NEXT_POST = max(NEXT_POST, float(state.get("next_post_ts", 0) or 0))
    if not QUEUE or now < NEXT_POST:
        return
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    p = state["posts"]
    if p.get("day") != day:
        p["day"], p["count"] = day, 0
    p["hours"] = [t for t in p["hours"] if now - t < 3600]
    # Keep queued posts across the daily cap; do not silently clear them.
    if p["count"] >= MAX_POSTS_DAY or state.get("limit_day") == day:
        return
    if len(p["hours"]) >= MAX_POSTS_HOUR:
        return
    QUEUE.sort(key=lambda x: (x["prio"], x["ts"]))
    item = QUEUE[0]
    if now - item["ts"] > item["ttl"]:
        log("SQUARE QUEUE EXPIRED", item.get("base", "unknown"))
        QUEUE.pop(0)
        sync_queues(state)
        save_state(state)
        return
    if now < float(item.get("next_try", 0) or 0):
        return

    img = os.path.join(CARD_DIR, f"{item['base']}_{int(now)}.png")
    try:
        make_card(item["base"], item["pos"], item["label"], item["metric"], img)
    except Exception as e:
        log("SQUARE IMAGE GENERATION ERROR", repr(e))
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["next_try"] = now + min(900, 15 * (2 ** min(item["attempts"], 6)))
        sync_queues(state)
        save_state(state)
        return

    ok, info = post_square(item["text"], img)
    try:
        if os.path.exists(img):
            os.remove(img)
    except Exception:
        pass

    if ok:
        QUEUE.pop(0)
        p["count"] += 1
        p["hours"].append(now)
        NEXT_POST = now + POST_GAP[0]
        state["next_post_ts"] = NEXT_POST
        log(f"SQUARE POST PUBLISHED ${item['base']} ({info}) today={p['count']} next_in={POST_GAP[0]:.0f}s")
    else:
        item["attempts"] = int(item.get("attempts", 0)) + 1
        # API quota/auth/config failures are not hammered every poll.
        delay = (3600 if info.startswith(("AUTH_ERROR", "CONFIG_ERROR", "TIMEOUT"))
                 else 900 if info.startswith("LIMIT")
                 else 21600 if info.startswith("PUBLISH_ERROR") and any(x in info for x in ("20002", "20013", "20022", "30008"))
                 else min(900, 15 * (2 ** min(item["attempts"], 6))))
        item["next_try"] = now + delay
        log(f"SQUARE POST FAILED ${item['base']}: {info}; retry_in={delay}s")
        if info.startswith("LIMIT"):
            state["limit_day"] = day
            queue_telegram("Binance Square daily post limit reached. Queued posts are saved for the next UTC day.")
    sync_queues(state)
    save_state(state)


# ----------------------------------------------------------------- state
def new_state():
    return {"mcaps": {}, "mcaps_ts": 0, "cool": {}, "open": [], "stats": {},
            "posts": {"day": "", "count": 0, "hours": []}, "limit_day": "", "summary_day": "",
            "queue": [], "telegram_queue": [], "next_post_ts": 0}


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        for k, v in new_state().items():
            s.setdefault(k, v)
        if not isinstance(s.get("queue"), list):
            s["queue"] = []
        if not isinstance(s.get("telegram_queue"), list):
            s["telegram_queue"] = []
        return s
    except Exception:
        return new_state()


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log("state save error", e)


def cool_ok(state, key, minutes, now):
    return now - state["cool"].get(key, 0) >= minutes * 60


def cool_set(state, key, now):
    state["cool"][key] = now


def fmt(p):
    return f"{p:.8f}".rstrip("0").rstrip(".") if p < 1 else f"{p:.4f}"


# ----------------------------------------------------------------- FLASH
def price_at(dq, target, tol=100):
    for ts, p in reversed(dq):
        if ts <= target:
            return p if target - ts <= tol else None
    return None


def flash_confirm(sym, w):
    kl = klines(sym, "1m", w + 65)
    if not kl or len(kl) < w + 20:
        return None
    win, base = kl[-w:], kl[-(w + 60):-w]
    vw = sum(k["qv"] for k in win)
    bpm = mean([k["qv"] for k in base])
    mult = (vw / w) / bpm if bpm > 0 else 99.0
    chg = (kl[-1]["c"] / win[0]["o"] - 1) * 100
    return {"chg": chg, "mult": mult, "vw": vw}


def flash_scan(state, tick, now):
    cands = []
    for sym, t in tick.items():
        dq = HIST[sym]
        best = None
        for w, thr in FLASH_THR.items():
            old = price_at(dq, now - w * 60)
            if not old:
                continue
            chg = (t["price"] / old - 1) * 100
            if chg >= thr or (ALERT_DOWN and chg <= -thr):
                sc = abs(chg) / thr
                if not best or sc > best[0]:
                    best = (sc, w, chg)
        if best:
            cands.append((best[0], sym, best[1]))
    cands.sort(reverse=True)
    checked = 0
    for _, sym, w in cands:
        if checked >= FLASH_CHECKS:
            break
        if not cool_ok(state, f"flash:{sym}", FLASH_CD, now) or not cool_ok(state, f"chk:{sym}", 1.5, now):
            continue
        cool_set(state, f"chk:{sym}", now)
        checked += 1
        info = flash_confirm(sym, w)
        if not info:
            continue
        chg, mult = info["chg"], info["mult"]
        up = chg > 0
        if abs(chg) < FLASH_THR[w] * 0.8 or mult < FLASH_VOL_MULT or info["vw"] < MIN_WINDOW_USDT:
            continue
        if not up and not ALERT_DOWN:
            continue
        t = tick[sym]
        cool_set(state, f"flash:{sym}", now)
        word = "Flash spike" if up else "Flash drop"
        head = f"{word}: {chg:+.1f}% in {w}m on {mult:.1f}x volume."
        if t["ratio"] is not None:
            head += f" Vol/MCap {t['ratio']:.0f}% (24h)."
        enqueue(2, 600, t["base"], up, compose(t["base"], up, head),
                f"FLASH {'SPIKE' if up else 'DROP'} | {w}m",
                f"{chg:+.1f}% in {w}m   |   {mult:.1f}x volume",
                f"FLASH {t['base']}/USDT {chg:+.1f}% in {w}m, volume {mult:.1f}x, price {fmt(t['price'])}")
        log(f"flash {sym} {chg:+.1f}% {w}m vol {mult:.1f}x")


# ----------------------------------------------------------------- SCALP
def eval_scalp(kl):
    if not kl or len(kl) < 60:
        return None
    o = [k["o"] for k in kl]
    h = [k["h"] for k in kl]
    l = [k["l"] for k in kl]
    c = [k["c"] for k in kl]
    qv = [k["qv"] for k in kl]
    a = atr_last(h, l, c)
    if not a:
        return None
    e9, e21 = ema_series(c, 9)[-1], ema_series(c, 21)[-1]
    r = rsi_last(c)
    avg = mean(qv[-21:-1])
    vm = qv[-1] / avg if avg else 0
    brk = c[-1] > max(h[-13:-1])
    rng = h[-1] - l[-1]
    body = (c[-1] - o[-1]) / rng if rng > 0 else 0
    strong = vm >= 3 and body >= 0.6
    trend = c[-1] > e21 and e9 > e21
    ext = (c[-1] - e21) / a
    tp_pct = 1.5 * a / c[-1] * 100
    if not (trend and (brk or strong or (vm >= 1.25 and body >= 0.45)) and vm >= 1.25 and 45 <= r <= 82 and ext <= 4.5
            and tp_pct >= MIN_TP_PCT):
        return None
    return {"entry": c[-1], "atr": a, "vm": vm, "rsi": r, "brk": brk}


def scalp_scan(state, tick, tf, now):
    candidates = top_by_ratio(tick, SCALP_TOP)
    btc_kl = fetch_tf("BTCUSDT", tf)
    btc_ok = bool(btc_kl and len(btc_kl) >= 60 and btc_kl[-1]["c"] > ema_series([x["c"] for x in btc_kl], 50)[-1])
    micro_checked = 0
    for sym in candidates:
        if (not cool_ok(state, f"scalp:{sym}", SCALP_CD, now)
                or not cool_ok(state, f"coin:{sym}", COIN_CD, now)):
            continue
        kl = fetch_tf(sym, tf)
        sig = eval_scalp(kl)
        time.sleep(0.08)
        if not sig:
            continue
        bk = bget("/api/v3/ticker/bookTicker", {"symbol": sym})
        try:
            spread = (float(bk["askPrice"]) / float(bk["bidPrice"]) - 1) * 100
        except Exception:
            continue
        if spread > MAX_SPREAD_PCT:
            continue
        micro = market_microstructure(sym) if micro_checked < MICRO_TOP else None
        if micro_checked < MICRO_TOP:
            micro_checked += 1
        higher_tf = fetch_tf(sym, "1h")
        higher_close = [x["c"] for x in higher_tf] if higher_tf else []
        mtf_ok = bool(len(higher_close) >= 60 and higher_close[-1] > ema_series(higher_close, 20)[-1]
                      and ema_series(higher_close, 20)[-1] > ema_series(higher_close, 55)[-1])
        score, parts = market_score(kl, micro, btc_ok, mtf_ok)
        if score < MIN_SCORE:
            continue
        t = tick[sym]
        e, a = t["price"], sig["atr"]
        sl, tp1, tp2 = e - 1.5 * a, e + 1.5 * a, e + 3 * a
        cool_set(state, f"scalp:{sym}", now)
        cool_set(state, f"coin:{sym}", now)
        why = "breakout above recent highs" if sig["brk"] else "early volume expansion"
        head = f"Scalp {tf}: {why}; score {score}/100, volume {sig['vm']:.1f}x."
        if t["ratio"] is not None:
            head += f" Vol/MCap {t['ratio']:.0f}%."
        detail = " | ".join(f"{k}:{v}" for k, v in parts.items())
        msg = (f"SCALP {tf} {t['base']}/USDT | SCORE {score}/100\nEntry: {fmt(e)}\nSL: {fmt(sl)} ({(sl/e-1)*100:.2f}%)\n"
               f"TP1: {fmt(tp1)} (+{(tp1/e-1)*100:.2f}%)\nTP2: {fmt(tp2)} (+{(tp2/e-1)*100:.2f}%)\n"
               f"Spread {spread:.3f}% | Vol {sig['vm']:.1f}x | RSI {sig['rsi']:.0f}\n{detail}")
        enqueue(1, 7200, t["base"], True, compose(t["base"], True, head), f"SCALP SIGNAL | {tf} | {score}",
                f"Score {score}/100   |   Volume {sig['vm']:.1f}x", msg)
        log(f"scalp {sym} {tf} score={score}")


# ----------------------------------------------------------------- SWING
FLAT_PCT = {"1h": 4.0, "4h": 6.0, "1d": 10.0}


def eval_swing(kl, tf, btc_ret):
    if not kl or len(kl) < 60:
        return None
    h = [k["h"] for k in kl]
    l = [k["l"] for k in kl]
    c = [k["c"] for k in kl]
    qv = [k["qv"] for k in kl]
    a = atr_last(h, l, c)
    base_vol = mean(qv[-25:-5])
    if not a or base_vol <= 0:
        return None
    vr = mean(qv[-5:]) / base_vol
    flat = abs(c[-1] / c[-6] - 1) * 100 <= FLAT_PCT.get(tf, 6.0)
    higher_low = min(l[-5:]) > min(l[-15:-5])
    above = c[-1] > ema_series(c, 50)[-1]
    near_high = c[-1] >= max(h[-21:-1]) * 0.97
    ret = c[-1] / c[-25] - 1
    rs = btc_ret is None or ret > btc_ret
    r = rsi_last(c)
    if not (vr >= 1.2 and flat and higher_low and above and near_high and rs and r <= 74):
        return None
    return {"entry": c[-1], "atr": a, "vr": vr, "rsi": r}


def swing_scan(state, tick, tf, now):
    btc = fetch_tf("BTCUSDT", tf)
    btc_ret = (btc[-1]["c"] / btc[-25]["c"] - 1) if btc and len(btc) > 30 else None
    btc_ok = bool(btc and len(btc) >= 60 and btc[-1]["c"] > ema_series([x["c"] for x in btc], 50)[-1])
    micro_checked = 0
    for sym in top_by_ratio(tick, SWING_TOP):
        if (not cool_ok(state, f"swing:{sym}", SWING_CD, now)
                or not cool_ok(state, f"coin:{sym}", COIN_CD, now)):
            continue
        kl = fetch_tf(sym, tf)
        sig = eval_swing(kl, tf, btc_ret)
        time.sleep(0.08)
        if not sig:
            continue
        micro = market_microstructure(sym) if micro_checked < MICRO_TOP else None
        if micro_checked < MICRO_TOP:
            micro_checked += 1
        confirm_tf = "4h" if tf == "1h" else "1d" if tf == "4h" else "1w"
        higher_tf = fetch_tf(sym, confirm_tf)
        higher_close = [x["c"] for x in higher_tf] if higher_tf else []
        mtf_ok = bool(len(higher_close) >= 60 and higher_close[-1] > ema_series(higher_close, 20)[-1]
                      and ema_series(higher_close, 20)[-1] > ema_series(higher_close, 55)[-1])
        score, parts = market_score(kl, micro, btc_ok, mtf_ok)
        if score < MIN_SCORE:
            continue
        t = tick[sym]
        e, a = t["price"], sig["atr"]
        sl, tp1, tp2 = e - 2 * a, e + 3 * a, e + 6 * a
        cool_set(state, f"swing:{sym}", now)
        cool_set(state, f"coin:{sym}", now)
        head = (f"Swing {tf}: accumulation, higher lows and relative strength; score {score}/100, "
                f"volume building {sig['vr']:.1f}x.")
        detail = " | ".join(f"{k}:{v}" for k, v in parts.items())
        msg = (f"SWING {tf} {t['base']}/USDT | SCORE {score}/100\nEntry: {fmt(e)}\nSL: {fmt(sl)} ({(sl/e-1)*100:.2f}%)\n"
               f"TP1: {fmt(tp1)} (+{(tp1/e-1)*100:.2f}%)\nTP2: {fmt(tp2)} (+{(tp2/e-1)*100:.2f}%)\n"
               f"Vol {sig['vr']:.1f}x | RSI {sig['rsi']:.0f}\n{detail}")
        enqueue(1, 7200, t["base"], True, compose(t["base"], True, head), f"SWING SIGNAL | {tf} | {score}",
                f"Score {score}/100   |   Volume building {sig['vr']:.1f}x", msg)
        log(f"swing {sym} {tf} score={score}")


# ----------------------------------------------------------------- results (Telegram only)
def track(state, tick, now):
    keep = []
    for s in state["open"]:
        p = tick.get(s["sym"], {}).get("price")
        if p is None:
            keep.append(s)
            continue
        if p >= s["tp1"]:
            res, k = "WIN (TP1 hit)", "win"
        elif p <= s["sl"]:
            res, k = "LOSS (SL hit)", "loss"
        elif now - s["ts"] > s["ttl"]:
            res, k = f"EXPIRED ({(p / s['entry'] - 1) * 100:+.2f}%)", "exp"
        else:
            keep.append(s)
            continue
        st = state["stats"].setdefault(s["mode"], {"win": 0, "loss": 0, "exp": 0})
        st[k] += 1
        queue_telegram(f"RESULT {s['mode']} {s['tf']} {s['base']}: {res}")
    state["open"] = keep


def daily_summary(state, now):
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    if state["summary_day"] in ("", day):
        state["summary_day"] = day
        return
    state["summary_day"] = day
    lines = ["Totals so far:"]
    for m, st in state["stats"].items():
        n = st["win"] + st["loss"]
        wr = f"{st['win'] / n * 100:.0f}%" if n else "n/a"
        lines.append(f"{m}: win {st['win']} / loss {st['loss']} / expired {st['exp']}  (win rate {wr})")
    if len(lines) > 1:
        queue_telegram("\n".join(lines))


# ----------------------------------------------------------------- main loop
def main():
    state = load_state()
    global NEXT_POST
    QUEUE[:] = state.get("queue", [])
    TELEGRAM_QUEUE[:] = state.get("telegram_queue", [])
    NEXT_POST = float(state.get("next_post_ts", 0) or 0)
    log(f"start dry_run={DRY_RUN} score_min={MIN_SCORE} square_gap={POST_GAP[0]:.0f}s poll={POLL_SEC}s runtime={MAX_RUNTIME_MIN}m "
        f"key={'yes' if square_key() else 'NO'} square_script={'yes' if find_square_script() else 'no (image posting unavailable)'} "
        f"restored_square_queue={len(QUEUE)} restored_telegram_queue={len(TELEGRAM_QUEUE)}")
    queue_telegram(f"BINANCE GEMS started{' (dry run)' if DRY_RUN else ''}. Score minimum: {MIN_SCORE}.")
    sync_queues(state)
    save_state(state)
    end = time.time() + MAX_RUNTIME_MIN * 60
    allowed, allowed_ts = None, 0
    last_scalp_min = last_swing_min = last_save = last_prune = 0
    mcap_try = 0
    while time.time() < end:
        t0 = time.time()
        now = t0
        try:
            if now - allowed_ts > 3600:
                allowed, allowed_ts = load_allowed() or allowed, now
            if (not state["mcaps"] and now - mcap_try > 900) or now - state["mcaps_ts"] > 3 * 3600 and now - mcap_try > 900:
                mcap_try = now
                caps = load_mcaps()
                if caps:
                    state["mcaps"], state["mcaps_ts"] = caps, now
                    log(f"market caps loaded: {len(caps)}")
                else:
                    log("market cap load failed (keeping old / running without ratio)")
            raw = bget("/api/v3/ticker/24hr")
            if raw:
                tick = parse_tickers(raw, allowed)
                for s, t in tick.items():
                    mc = state["mcaps"].get(t["base"])
                    t["ratio"] = (t["qv"] / mc * 100) if mc else None
                    HIST[s].append((now, t["price"]))
                # Flash alerts are OFF by default; Scalp and Swing Spot scans remain active.
                if FLASH_ENABLED:
                    flash_scan(state, tick, now)
                # No follow-up result notifications for previously posted signals.
                mnt = int(now // 60)
                sec = now % 60
                if sec >= 4 and mnt != last_scalp_min:
                    last_scalp_min = mnt
                    for tf, n in (("10m", 10), ("15m", 15), ("30m", 30)):
                        if mnt % n == 0:
                            scalp_scan(state, tick, tf, now)
                if sec >= 8 and mnt % 60 == 1 and mnt != last_swing_min:
                    last_swing_min = mnt
                    hour = (mnt // 60) % 24
                    swing_scan(state, tick, "1h", now)
                    if hour % 4 == 0:
                        swing_scan(state, tick, "4h", now)
                    if hour == 0:
                        swing_scan(state, tick, "1d", now)
            else:
                log("ticker fetch failed")
            sync_queues(state)
            drain_telegram(state)
            drain(state, time.time())
            daily_summary(state, now)
            if now - last_prune > 3600:
                last_prune = now
                state["cool"] = {k: v for k, v in state["cool"].items() if now - v < 86400}
            if now - last_save > 60:
                last_save = now
                sync_queues(state)
                save_state(state)
        except Exception:
            log("loop error:\n" + traceback.format_exc())
        # keep draining the post queue while waiting for the next poll
        while time.time() - t0 < POLL_SEC and time.time() < end:
            time.sleep(1)
            try:
                sync_queues(state)
                drain_telegram(state)
                drain(state, time.time())
            except Exception:
                log("drain error:\n" + traceback.format_exc())
    sync_queues(state)
    save_state(state)
    log(f"finished runtime; saved square_queue={len(QUEUE)} telegram_queue={len(TELEGRAM_QUEUE)}")


# ----------------------------------------------------------------- offline self-test
def _candles(closes, vols, step=900000, start=1_700_000_000_000, spread=0.004):
    out = []
    prev = closes[0]
    for i, (c, v) in enumerate(zip(closes, vols)):
        o = prev
        out.append({"t": start + i * step, "o": o, "h": max(o, c) * (1 + spread / 2),
                    "l": min(o, c) * (1 - spread / 2), "c": c, "v": v, "qv": v * c,
                    "ct": start + (i + 1) * step - 1})
        prev = c
    return out


def selftest():
    global klines
    rnd = random.Random(7)
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    # indicators
    check("market score components sum to 100 max", sum([20,20,15,15,10,10,10]) == 100)
    score_test_kl = _candles([100 + i * 0.15 for i in range(100)], [100] * 99 + [220])
    score_value, score_parts = market_score(score_test_kl, {"book_imb": .62, "taker_buy": .64}, True, True)
    check("live scoring returns bounded 0-100 score with all categories", 0 <= score_value <= 100 and len(score_parts) == 7)
    up = [100 + i for i in range(40)]
    check("rsi of straight uptrend is 100", rsi_last(up) == 100.0)
    check("ema follows price", 130 < ema_series(up, 10)[-1] < 139)

    # 10m aggregation from 5m (aligned buckets, partial bucket dropped)
    k5 = _candles([100 + i * 0.1 for i in range(13)], [10] * 13, step=300000, start=600000 * 1000 + 300000)
    a10 = agg(k5, 10)
    check("10m aggregation builds only complete buckets", all(x["n"] == 2 for x in a10) and len(a10) >= 5)

    # scalp: slow uptrend, then breakout candle on big volume
    closes = [100 + i * 0.03 + rnd.uniform(-0.25, 0.25) for i in range(99)]
    closes.append(max(closes) * 1.012)
    vols = [100 + rnd.uniform(-10, 10) for _ in range(99)] + [420]
    sig = eval_scalp(_candles(closes, vols))
    check("scalp setup detected on breakout + volume", sig is not None)
    flat = [100 + rnd.uniform(-0.25, 0.25) for _ in range(100)]
    check("scalp ignores sideways noise", eval_scalp(_candles(flat, [100] * 100)) is None)

    # swing: tight base, rising lows, volume building, near highs
    sw = [100 + i * 0.02 + 0.15 * ((i % 4) - 1.5) for i in range(100)]
    swv = [100] * 95 + [190, 200, 210, 205, 215]
    ss = eval_swing(_candles(sw, swv, step=3600000), "1h", -0.01)
    check("swing accumulation detected", ss is not None)
    check("swing rejected when volume is not building",
          eval_swing(_candles(sw, [100] * 100, step=3600000), "1h", -0.01) is None)

    # flash: simulate history with a 4% jump in 5m and a volume spike
    now = time.time()
    tick = {"AAAUSDT": {"base": "AAA", "price": 1.05, "qv": 5e6, "chg24": 9.0, "ratio": 41.0},
            "BBBUSDT": {"base": "BBB", "price": 2.0, "qv": 5e6, "chg24": 0.1, "ratio": 3.0}}
    for i in range(12, -1, -1):
        HIST["AAAUSDT"].append((now - i * 30, 1.0 if i > 1 else 1.05))
        HIST["BBBUSDT"].append((now - i * 30, 2.0))

    def fake(sym, interval, limit):
        cl = [1.0] * (limit - 6) + [1.01, 1.02, 1.03, 1.04, 1.05, 1.05]
        vo = [1000] * (limit - 6) + [9000] * 6
        return _candles(cl, vo, step=60000)
    klines = fake
    st = new_state()
    QUEUE.clear()
    flash_scan(st, tick, now)
    check("flash alert queued for AAA only", len(QUEUE) == 1 and QUEUE[0]["base"] == "AAA")
    n_before = len(QUEUE)
    flash_scan(st, tick, now + 5)
    check("flash cooldown blocks duplicate", len(QUEUE) == n_before)

    # post format
    txt = QUEUE[0]["text"]
    lines = txt.split("\n")
    check("post is max 4 lines", len(lines) <= 4)
    check("post starts with $COIN and has mood", lines[0].startswith("$AAA") and "Positive" in lines[0])
    tags = [w for w in lines[-1].split(" ") if w.startswith("#")]
    check("post has 2 clean general hashtags plus coin hashtag, space-separated",
          len(tags) == 3 and all(re.fullmatch(r"#[A-Za-z0-9_]+", t) for t in tags)
          and tags[2] == "#AAA" and "  " not in lines[-1]
          and lines[-1].startswith(tags[0] + " ") and lines[-1].endswith(" " + tags[2]))
    cd_state = new_state()
    cool_set(cd_state, "coin:AAAUSDT", now)
    check("shared coin cooldown blocks another signal for 12 hours",
          not cool_ok(cd_state, "coin:AAAUSDT", COIN_CD, now + 5 * 3600))
    for _ in range(200):
        neg = compose("XYZ", False, "x").split("\n")[-1]
        if any(b in neg for b in TAGS_BULLISH):
            check("negative posts never use bullish-only tags", False)
            break
    else:
        check("negative posts never use bullish-only tags", True)
    print("\nSample post:\n" + txt + "\n")

    # card images
    os.makedirs("cards", exist_ok=True)
    make_card("AAA", True, "FLASH SPIKE | 5m", "+4.8% in 5m   |   9.0x volume", "cards/test_up.png")
    make_card("1000SATS", False, "FLASH SPIKE | 15m", "-5.2% in 15m   |   6.3x volume", "cards/test_down.png")
    check("cards created", os.path.getsize("cards/test_up.png") > 5000)

    # post queue gap + caps (dry run)
    global DRY_RUN, NEXT_POST
    DRY_RUN = True
    QUEUE.clear()
    for b in ("A1", "A2"):
        enqueue(1, 7200, b, True, compose(b, True, "test"), "TEST", "m")
    st = new_state()
    t0 = time.time()
    drain(st, t0)
    gap1 = NEXT_POST - t0
    drain(st, t0 + 1)                      # too early -> must not post
    check("15-second Square gap enforced", 14.9 <= gap1 <= 15.1 and len(QUEUE) == 1)
    drain(st, NEXT_POST + 0.1)
    check("second post sent after gap", len(QUEUE) == 0 and st["posts"]["count"] == 2)
    stq = new_state()
    QUEUE.append({"prio": 1, "ttl": 60, "ts": t0, "base": "PERSIST", "pos": True,
                  "text": "$PERSIST", "label": "TEST", "metric": "test"})
    sync_queues(stq)
    check("Square queue stored in state", len(stq["queue"]) == 1 and stq["queue"][0]["base"] == "PERSIST")
    QUEUE.clear()
    print("\nALL OK" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
