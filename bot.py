#!/usr/bin/env python3
"""
Binance SPOT coin bot -> Binance Square + Telegram

Modes:
  FLASH  5/10/15/30m  sudden price + volume spike
  SCALP  10/15/30m    breakout setups
  SWING  1h/4h/1d     accumulation / trend continuation (new conditions)

Only pure crypto coins (no stables, no leveraged tokens, no tokenized stocks).
Posts have high variety in structure/style so they don't look bot-like.
Image card style remains the same as original.
"""

import os
import sys
import re
import json
import time
import random
import subprocess
import glob
import shutil
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
MAX_RUNTIME_MIN = envf("MAX_RUNTIME_MIN", 340)
DRY_RUN = envb("DRY_RUN", False)
STATE_FILE = envs("STATE_FILE", "state.json")
CARD_DIR = envs("CARD_DIR", "cards")

FLASH_THR = {5: envf("FLASH_5M", 3.0), 10: envf("FLASH_10M", 4.0),
             15: envf("FLASH_15M", 5.0), 30: envf("FLASH_30M", 6.0)}
FLASH_VOL_MULT = envf("FLASH_VOL_MULT", 5.0)
MIN_WINDOW_USDT = envf("MIN_WINDOW_USDT", 2000)
ALERT_DOWN = envb("ALERT_DOWN", False)
FLASH_CHECKS = 8

SCALP_TOP = int(envf("SCALP_TOP", 40))
SWING_TOP = int(envf("SWING_TOP", 60))
MIN_TP_PCT = envf("MIN_TP_PCT", 0.5)
MAX_SPREAD_PCT = envf("MAX_SPREAD_PCT", 0.15)
MIN_CONF = int(envf("MIN_CONF", 4))                # extra checks that must pass (scalp/swing)
BTC_MAX_DROP = envf("BTC_MAX_DROP", 1.0)           # BTC 1h drop (%) that blocks scalp/swing signals
REQUIRE_IMAGE = envb("REQUIRE_IMAGE", False)       # True = never post without the image card

FLASH_CD = envf("FLASH_COOLDOWN_MIN", 45)
SCALP_CD = envf("SCALP_COOLDOWN_MIN", 60)
SWING_CD = envf("SWING_COOLDOWN_MIN", 720)

POST_GAP = (8.0, 15.0)
MAX_POSTS_DAY = int(envf("MAX_POSTS_DAY", 95))
MAX_POSTS_HOUR = int(envf("MAX_POSTS_HOUR", 10))

HOSTS = ["https://data-api.binance.vision", "https://api.binance.com",
         "https://api1.binance.com", "https://api2.binance.com"]
GECKO = "https://api.coingecko.com/api/v3/coins/markets"
SQUARE_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"

# Pure crypto only
STABLES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDD", "EUR", "GBP", "TRY",
    "BRL", "AEUR", "USD1", "XUSD", "PYUSD", "UST", "USTC", "RLUSD", "BFUSD", "USDE",
    "EURC", "EURS", "SUSD", "GUSD", "HUSD"
}

# Leveraged / tokenized patterns to exclude
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")
TOKENIZED_KEYWORDS = ("BSTOCK", "STOCK", "TSLA", "AAPL", "GOOGL", "AMZN", "MSFT", "NVDA", "META")

TAGS_NEUTRAL = ["#spottrading", "#signal", "#Spot", "#SpotTradingSuccess",
                "#TradingSignal", "#TradingTips", "#SignalAlert", "#CryptoSignal"]
TAGS_BULLISH = ["#SPOTCALL🔥🔥🔥", "#BullishMomentum", "#BullishSetup"]

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (coin-bot)"
HOST_IDX = 0
HIST = defaultdict(lambda: deque(maxlen=int(2000 / max(POLL_SEC, 5)) + 20))
QUEUE = []
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
            if r.status_code in (451, 403):
                HOST_IDX += 1
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            HOST_IDX += 1
            time.sleep(1)
    return None


def tg(text):
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if DRY_RUN or not tok or not chat:
        if DRY_RUN:
            print("[TG]", text)
        return
    try:
        S.post(f"https://api.telegram.org/bot{tok}/sendMessage",
               data={"chat_id": chat, "text": text[:4000]}, timeout=20)
    except Exception as e:
        log("telegram error", e)


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


# ----------------------------------------------------------------- binance data
def klines(symbol, interval, limit):
    d = bget("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not d:
        return None
    return [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
             "c": float(k[4]), "v": float(k[5]), "qv": float(k[7]), "tb": float(k[10]), "ct": k[6]} for k in d]


def closed(kl):
    now_ms = int(time.time() * 1000)
    return [k for k in kl if k["ct"] < now_ms]


def agg(kl5, minutes):
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
            cur["tb"] = cur.get("tb", 0.0) + k.get("tb", 0.0)
            cur["ct"] = k["ct"]
            cur["n"] += 1
    if cur:
        out.append(cur)
    return [x for x in out if x["n"] == minutes // 5]


def fetch_tf(sym, tf):
    if tf == "10m":
        kl = klines(sym, "5m", 300)
        return agg(closed(kl), 10) if kl else None
    kl = klines(sym, tf, 210)
    return closed(kl) if kl else None


def is_pure_crypto(base: str) -> bool:
    """Only pure crypto coins. Exclude stables, leveraged, tokenized stocks."""
    if not base or base in STABLES:
        return False
    upper = base.upper()
    if any(upper.endswith(suf) for suf in LEVERAGED_SUFFIXES):
        return False
    if any(kw in upper for kw in TOKENIZED_KEYWORDS):
        return False
    # common leveraged / synthetic patterns
    if re.search(r"(UP|DOWN|BULL|BEAR|3L|3S|5L|5S)$", upper):
        return False
    return True


def parse_tickers(raw, allowed=None):
    out = {}
    for t in raw:
        s = t.get("symbol", "")
        if not s.endswith("USDT") or (allowed is not None and s not in allowed):
            continue
        base = s[:-4]
        if not is_pure_crypto(base):
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
          if x.get("status") == "TRADING" and x.get("quoteAsset") == "USDT"}
    return ok or None


def load_mcaps():
    caps, best = {}, {}
    hdr = {}
    if os.getenv("COINGECKO_API_KEY"):
        hdr["x-cg-demo-api-key"] = os.getenv("COINGECKO_API_KEY")
    for page in range(1, 7):
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
    if len(ranked) < n:
        rest = sorted((s for s in tick if s not in ranked),
                      key=lambda s: tick[s]["qv"], reverse=True)
        ranked += rest[:n - len(ranked)]
    return ranked


# ----------------------------------------------------------------- posts (high variety)
def pick_tags(positive):
    pool = TAGS_NEUTRAL + (TAGS_BULLISH if positive else [])
    return random.sample(pool, 2)


MOOD_POS = [
    "🟢 Positive mood", "Positive mood 🟢", "Bullish mood 🟢",
    "Mood: positive 🟢", "Mood: bullish 📈", "Looking constructive 🟢",
    "Buyers in control 🟢", "Momentum turning up 📈"
]
MOOD_NEG = [
    "🔴 Negative mood", "Negative mood 🔴", "Bearish mood 🔴",
    "Mood: negative 🔴", "Mood: bearish 📉", "Sellers active 🔴",
    "Pressure on the downside 📉"
]

RATIO_VARIANTS = [
    " Vol/MCap {r}% (24h).",
    " 24h volume is {r}% of market cap.",
    " 24h Vol/MCap: {r}%.",
    " Today's volume = {r}% of market cap.",
    " Volume standing at {r}% of mcap."
]
CHECK_VARIANTS = [" Checked: {c}.", " Confirmed by {c}.", " Aligned: {c}.", " In line: {c}."]

# Many different structures so posts don't look the same
TEMPLATES = {
    "flash_up": [
        "{B} {mood}\nUp {absc} in {w}m on {mult}x volume.{ratio}",
        "Heads up on {B}: {chg} in just {w} minutes, volume running {mult}x above normal.\n{mood}",
        "🔥 {B} is waking up.\n{w}m move: {chg}. Volume {mult}x.{ratio}\n{mood}",
        "Radar alert ⚡ {B}\nPrice {chg} / {w}m, volume {mult}x its usual pace.\n{mood}",
        "{B} update 📈\nBuyers stepped in: {chg} in {w}m with {mult}x volume.{ratio}\n{mood}",
        "Sudden move spotted 👀 {B} {chg} in {w}m.\nVolume is {mult}x normal.\n{mood}",
        "{B} just popped {chg} within {w}m 🚀\n{mult}x volume behind it.{ratio}\n{mood}",
        "Momentum check: {B}\n{chg} in {w}m · {mult}x volume · {mood}",
        "Spike alert! {B} jumped {chg} in {w} minutes.\nTrading volume {mult}x higher than usual.\n{mood}",
        "Quick note on {B}\nStrong {w}m candle: {chg} with {mult}x volume.\n{mood}",
        "{B} showing life.\n{chg} move in {w} minutes, volume elevated at {mult}x.{ratio}",
        "Watch {B} closely.\nPrice pushed {chg} in {w}m on {mult}x volume.\n{mood}",
        "Volume spike + price jump on {B}.\n{chg} in {w}m · {mult}x volume\n{mood}",
        "{B} | {w}m | {chg} | {mult}x vol\n{mood}",
        "Interesting print on {B}: +{absc} in {w} minutes while volume sits at {mult}x normal levels.\n{mood}",
    ],
    "flash_down": [
        "{B} {mood}\nDown {absc} in {w}m on {mult}x volume.{ratio}",
        "Heads up on {B}: {chg} in just {w} minutes, volume running {mult}x above normal.\n{mood}",
        "⚠️ {B} is dropping fast.\n{w}m move: {chg}. Volume {mult}x.{ratio}\n{mood}",
        "Radar alert 🔻 {B}\nPrice {chg} / {w}m, selling volume {mult}x its usual pace.\n{mood}",
        "{B} update 📉\nSellers stepped in: {chg} in {w}m with {mult}x volume.{ratio}\n{mood}",
        "Sharp dip spotted 👀 {B} fell {absc} in {w}m.\nVolume is {mult}x normal.\n{mood}",
        "{B} just slipped {absc} within {w}m 🔻\n{mult}x volume behind the move.{ratio}\n{mood}",
        "Momentum check: {B}\n{chg} in {w}m · {mult}x volume · {mood}",
        "Dump alert! {B} lost {absc} in {w} minutes.\nTrading volume {mult}x higher than usual.\n{mood}",
        "Quick note on {B}\nHeavy {w}m candle: {chg} with {mult}x volume.\n{mood}",
        "{B} under pressure.\n{chg} move in {w} minutes, volume elevated at {mult}x.{ratio}",
        "Watch {B}.\nPrice dropped {chg} in {w}m on {mult}x volume.\n{mood}",
        "Volume spike + downside move on {B}.\n{chg} in {w}m · {mult}x volume\n{mood}",
        "{B} | {w}m | {chg} | {mult}x vol\n{mood}",
        "Heavy selling print on {B}: -{absc} in {w} minutes while volume sits at {mult}x normal levels.\n{mood}",
    ],
    "scalp": [
        "{B} {mood}\nScalp setup on the {tf} chart: {why}, volume {vm}x.{ratio}",
        "Scalp watch ⚡ {B} ({tf})\n{whyc}, volume {vm}x, trend still up.\n{mood}",
        "{tf} scalp radar 🎯 {B}\nTrend is up, {why}, {vm}x volume.{ratio}\n{mood}",
        "{B} is lining up on the {tf} timeframe.\n{whyc} with {vm}x volume.\n{mood}",
        "Quick-trade candidate 👀 {B}\n{tf}: {why}, uptrend intact, volume {vm}x.\n{mood}",
        "{mood}\n{B} shows a {tf} scalp setup: {why} and {vm}x volume.{ratio}",
        "Eyes on {B} 🔥 {tf} chart\n{whyc} + {vm}x volume, trend up.\n{mood}",
        "{B} · {tf} scalp · {why} · {vm}x volume · {mood}",
        "Short-term setup: {B} on {tf}.\n{whyc}, volume {vm}x above average.{ratio}\n{mood}",
        "Possible scalp on {B} ({tf})\n{why} + elevated volume ({vm}x).\n{mood}",
        "{B} looking ready for a quick move on {tf}.\n{whyc}. Volume {vm}x.\n{mood}",
        "Clean {tf} structure on {B}.\n{why}, volume confirmation at {vm}x.{ratio}",
        "Scalp idea → {B}\nTimeframe {tf} | {why} | Vol {vm}x\n{mood}",
        "Noticing a setup on {B}.\n{tf} chart shows {why} with {vm}x volume.\n{mood}",
        "{B} | {tf} | {why} | {vm}x vol\n{mood}",
    ],
    "swing": [
        "{B} {mood}\nSwing watch ({tf}): volume building {vr}x while price stays constructive.{ratio}",
        "Accumulation signs on {B} 👀 ({tf})\nVolume up {vr}x, higher lows, holding structure.\n{mood}",
        "{tf} swing radar 📊 {B}\nQuiet price action, rising volume ({vr}x), higher lows.{ratio}\n{mood}",
        "{B} is coiling on the {tf} chart.\nVolume {vr}x and structure remains intact.\n{mood}",
        "Swing candidate: {B} ({tf})\nTight range, higher lows, {vr}x volume build-up.\n{mood}",
        "{mood}\n{B} looks like it is building a base on {tf}: higher lows and {vr}x volume.{ratio}",
        "Patience setup ⏳ {B} {tf}\nVolume {vr}x, price steady, relative strength visible.\n{mood}",
        "{B} · {tf} swing · volume {vr}x · higher lows · {mood}",
        "Base-building on {B} ({tf}).\nRising volume ({vr}x), steady price, higher lows.{ratio}\n{mood}",
        "Longer-term look at {B} on {tf}.\nVolume expanding {vr}x while price holds higher lows.\n{mood}",
        "{B} showing constructive price action on {tf}.\nVolume {vr}x above recent average.{ratio}",
        "Structure + volume alignment on {B} ({tf}).\nHigher lows present, volume {vr}x.\n{mood}",
        "Swing idea forming on {B}.\nTimeframe: {tf} | Volume build: {vr}x\n{mood}",
        "Keeping an eye on {B}.\n{tf} chart has higher lows and {vr}x volume.\n{mood}",
        "{B} | {tf} | Vol {vr}x | Higher lows\n{mood}",
        "Quiet strength in {B} on the {tf}.\nVolume rising ({vr}x) without aggressive price expansion.{ratio}",
        "Possible swing entry zone developing on {B} ({tf}).\nVolume confirmation at {vr}x.\n{mood}",
    ],
}

RECENT = defaultdict(lambda: deque(maxlen=6))


def _pick_template(key):
    pool = TEMPLATES[key]
    free = [i for i in range(len(pool)) if i not in RECENT[key]] or list(range(len(pool)))
    i = random.choice(free)
    RECENT[key].append(i)
    return pool[i]


def space_after_coin(text):
    """Binance Square turns $COIN into a coin tag only when a space (or line end) follows the name.
    Makes sure every $COIN is followed by exactly one space and drops punctuation glued to it."""
    text = re.sub(r"(\$[A-Za-z0-9]+)[:.,;!?]+(?=\s|$)", r"\1", text)   # "$AAA:" -> "$AAA"
    text = re.sub(r"(\$[A-Za-z0-9]+)(?=\s|$)", r"\1 ", text)            # "$AAA" -> "$AAA "
    return re.sub(r"(\$[A-Za-z0-9]+)  +", r"\1 ", text)                 # never two spaces


def compose(kind, base, positive, f):
    key = ("flash_up" if positive else "flash_down") if kind == "flash" else kind
    ratio = ""
    names = list(f.get("checks") or [])
    if names and random.random() < 0.5:
        pick = random.sample(names, min(5, len(names)))
        pick.sort(key=names.index)
        ratio = random.choice(CHECK_VARIANTS).format(c=", ".join(pick))
    elif f.get("ratio") is not None and random.random() < 0.60:
        ratio = random.choice(RATIO_VARIANTS).format(r=f"{f['ratio']:.0f}")
    chg = float(f.get("chg", 0.0))
    why = f.get("why", "")
    tpl = _pick_template(key)
    if "{mood}" not in tpl:                         # every post must say if the mood is positive/negative
        tpl += "\n{mood}"
    body = tpl.format(
        B=f"${base}", mood=random.choice(MOOD_POS if positive else MOOD_NEG), ratio=ratio,
        chg=f"{chg:+.1f}%", absc=f"{abs(chg):.1f}%", w=f.get("w", ""), tf=f.get("tf", ""),
        mult=f"{float(f.get('mult', 0)):.1f}", vm=f"{float(f.get('vm', 0)):.1f}",
        vr=f"{float(f.get('vr', 0)):.1f}", why=why, whyc=why[:1].upper() + why[1:] if why else "")
    tags = pick_tags(positive) + [f"#{base}"]          # 2 random hashtags + the coin's own hashtag
    random.shuffle(tags)
    lines = body.split("\n")
    lines.append(" ".join(tags) + " ")
    return space_after_coin("\n".join(lines[:4]))


# ----------------------------------------------------------------- card (same style as original)
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


PAL_UP = [((12, 46, 30), (4, 12, 9), (46, 214, 120)),
          ((8, 40, 48), (3, 12, 18), (40, 220, 200)),
          ((22, 46, 14), (7, 15, 5), (170, 230, 60)),
          ((10, 32, 62), (3, 9, 24), (70, 170, 255)),
          ((14, 50, 44), (4, 16, 14), (60, 235, 160))]
PAL_DOWN = [((56, 16, 20), (14, 5, 7), (255, 84, 84)),
            ((54, 26, 10), (14, 7, 3), (255, 140, 60)),
            ((48, 12, 40), (13, 4, 11), (240, 80, 170)),
            ((42, 14, 14), (10, 3, 3), (255, 112, 92))]
HEAD_UP = ["POSITIVE MOOD", "BULLISH VIBES", "BUYERS ACTIVE", "MOMENTUM UP"]
HEAD_DOWN = ["NEGATIVE MOOD", "BEARISH VIBES", "SELLERS ACTIVE", "PRESSURE DOWN"]
DISCLAIMERS = ["Not financial advice  |  Trade at your own risk", "DYOR  -  not financial advice",
               "Trade with care. Not financial advice."]


def _deco(od, kind, positive, acc, W, H, rnd, alpha):
    a = acc + (alpha,)
    if kind == "arrow":
        if positive:
            od.polygon([(930, 90), (1150, 400), (1010, 400), (1010, 560), (850, 560), (850, 400), (710, 400)],
                       fill=a)
        else:
            od.polygon([(930, 560), (1150, 250), (1010, 250), (1010, 90), (850, 90), (850, 250), (710, 250)],
                       fill=a)
    elif kind == "bars":
        n, bw, gap, x0 = 9, 46, 14, 600
        for i in range(n):
            frac = (i + 1) / n if positive else 1 - i / n
            hgt = int((140 + 360 * frac) * rnd.uniform(0.8, 1.0))
            x = x0 + i * (bw + gap)
            od.rectangle([x, H - 50 - hgt, x + bw, H - 50], fill=a)
    elif kind == "line":
        pts, y = [], (H - 130 if positive else 130)
        for i in range(12):
            y = min(H - 70, max(70, y + (-28 if positive else 28) + rnd.uniform(-45, 45)))
            pts.append((600 + i * 52, y))
        strong = acc + (min(255, alpha * 2),)
        od.line(pts, fill=strong, width=12, joint="curve")
        for px, py in pts:
            od.ellipse([px - 9, py - 9, px + 9, py + 9], fill=strong)
    else:
        cx, cy = rnd.randint(880, 1060), rnd.randint(220, 420)
        for r in (80, 160, 240, 320):
            od.ellipse([cx - r, cy - r, cx + r, cy + r], outline=a, width=10)


def make_card(base, positive, label, metric, path):
    from PIL import Image, ImageDraw, ImageChops
    rnd = random.Random()
    W, H = 1200, 630
    top, bot, acc = rnd.choice(PAL_UP if positive else PAL_DOWN)
    style = rnd.choice(["v", "h", "d"])
    mv = Image.linear_gradient("L").resize((W, H))
    mh = mv.rotate(90, expand=True).resize((W, H))
    mask = mv if style == "v" else mh if style == "h" else ImageChops.add(mv, mh, scale=2)
    img = Image.composite(Image.new("RGB", (W, H), bot), Image.new("RGB", (W, H), top), mask)

    center = rnd.random() < 0.35
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _deco(ImageDraw.Draw(ov), rnd.choice(["arrow", "bars", "line", "rings"]), positive, acc, W, H, rnd,
          34 if center else 50)
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    d = ImageDraw.Draw(img)
    if center:
        d.rectangle([0, 0, W, 14], fill=acc)
    else:
        d.rectangle([0, 0, 18, H], fill=acc)

    def X(w):
        return (W - w) // 2 if center else 70

    f = _font(34)
    pw = d.textbbox((0, 0), label, font=f)[2] + 50
    px = X(pw)
    if rnd.random() < 0.5:
        d.rounded_rectangle([px, 56, px + pw, 118], radius=31, fill=acc)
        d.text((px + 25, 63), label, font=f, fill=(8, 14, 12))
    else:
        d.rounded_rectangle([px, 56, px + pw, 118], radius=31, outline=acc, width=4)
        d.text((px + 25, 63), label, font=f, fill=acc)

    coin, size = f"${base}", 200
    while size > 60 and d.textbbox((0, 0), coin, font=_font(size))[2] > 1000:
        size -= 10
    cw = d.textbbox((0, 0), coin, font=_font(size))[2]
    d.text((X(cw), 150), coin, font=_font(size), fill=(255, 255, 255))

    head = rnd.choice(HEAD_UP if positive else HEAD_DOWN)
    hf = _font(56)
    hw = d.textbbox((0, 0), head, font=hf)[2]
    x0, y = X(hw + 85), 395
    if positive:
        d.polygon([(x0, y + 56), (x0 + 60, y + 56), (x0 + 30, y + 6)], fill=acc)
    else:
        d.polygon([(x0, y + 6), (x0 + 60, y + 6), (x0 + 30, y + 56)], fill=acc)
    d.text((x0 + 85, y - 2), head, font=hf, fill=acc)

    mf = _font(44)
    mw = d.textbbox((0, 0), metric, font=mf)[2]
    d.text((X(mw), 478), metric, font=mf, fill=(235, 240, 238))
    dt, df = rnd.choice(DISCLAIMERS), _font(26)
    dw = d.textbbox((0, 0), dt, font=df)[2]
    d.text((X(dw), 568), dt, font=df, fill=(150, 160, 156))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    img.save(path, "PNG")
    return path


def square_key():
    return os.getenv("BINANCE_SQUARE_OPENAPI_KEY") or os.getenv("SQUARE_API_KEY") or ""


IMG_ERR = {"ts": 0.0}


def note_image_error(msg):
    """Log it, and tell Telegram (at most every 30 min) why a post went out without its image."""
    log("IMAGE PROBLEM:", msg[:900].replace("\n", " "))
    now = time.time()
    if now - IMG_ERR["ts"] > 1800:
        IMG_ERR["ts"] = now
        tg("Binance Square: a post went out WITHOUT its image.\nReason: " + msg[:1500])


def find_square_script():
    d = envs("SQUARE_SKILL_DIR", "square-skill")
    hits = sorted(glob.glob(os.path.join(d, "**", "post-image.mjs"), recursive=True),
                  key=lambda p: (0 if "square-post" in p else 1, len(p)))
    return os.path.abspath(hits[0]) if hits else None


def _run_square_script(script, text, img_arg, cwd, env):
    p = subprocess.run(["node", script, "--text", text, "--images", img_arg],
                       capture_output=True, text=True, timeout=180, env=env, cwd=cwd)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    return (p.returncode == 0 or "Success" in out), out


def post_image_square(text, img, key):
    """Binance's official post-image.mjs, tried two ways: (1) png, run from the skill folder,
    (2) jpg copy, run from the image folder. -> (ok, info, error_text)"""
    script = find_square_script()
    if not script:
        return False, "", "post-image.mjs not found (square-skill clone failed?)"
    if not shutil.which("node"):
        return False, "", "node is not installed on this machine"
    env = dict(os.environ, BINANCE_SQUARE_OPENAPI_KEY=key)
    png = os.path.abspath(img)
    attempts = [("png, from skill folder", png, os.path.dirname(os.path.dirname(script)))]
    jpg = os.path.splitext(png)[0] + ".jpg"
    try:
        from PIL import Image
        Image.open(png).convert("RGB").save(jpg, "JPEG", quality=92)
        attempts.append(("jpg, from image folder", os.path.basename(jpg), os.path.dirname(jpg)))
    except Exception as e:
        log("jpg copy skipped:", e)
    errs = []
    try:
        for name, arg, cwd in attempts:
            try:
                ok, out = _run_square_script(script, text, arg, cwd, env)
            except Exception as e:
                ok, out = False, f"exception: {e}"
            if ok:
                return True, "image", ""
            if "220009" in out:
                return False, "LIMIT", out
            errs.append(f"[{name}] {out[-450:]}")
    finally:
        try:
            os.remove(jpg)
        except Exception:
            pass
    return False, "", " | ".join(errs)


def post_square(text, img):
    if DRY_RUN:
        print("---- SQUARE POST (dry run) ----\n" + text + f"\n[image: {img}]\n")
        return True, "dry"
    key = square_key()
    if not key:
        return False, "no BINANCE_SQUARE_OPENAPI_KEY"
    if img and os.path.exists(img):
        ok, info, err = post_image_square(text, img, key)
        if ok:
            return True, "image"
        if info == "LIMIT":
            return False, "LIMIT"
        note_image_error(err)
        if REQUIRE_IMAGE:
            return False, "image failed (REQUIRE_IMAGE is on, not posting text-only)"
    try:
        r = S.post(SQUARE_URL, json={"bodyTextOnly": text}, timeout=25, headers={
            "X-Square-OpenAPI-Key": key, "Content-Type": "application/json",
            "clienttype": "binanceSkill"})
        j = r.json()
        code = str(j.get("code"))
        if code == "000000":
            return True, "text"
        if code == "220009":
            return False, "LIMIT"
        return False, f"{code} {j.get('message')}"
    except Exception as e:
        return False, f"error {e}"


def enqueue(prio, ttl, base, positive, text, label, metric, tgmsg=None):
    if len(QUEUE) >= 40:
        QUEUE.sort(key=lambda x: (x["prio"], x["ts"]))
        QUEUE.pop()
    QUEUE.append({"prio": prio, "ttl": ttl, "ts": time.time(), "base": base, "pos": positive,
                  "text": text, "label": label, "metric": metric})
    if tgmsg:
        tg(tgmsg)


def drain(state, now):
    global NEXT_POST
    if not QUEUE or now < NEXT_POST:
        return
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    p = state["posts"]
    if p.get("day") != day:
        p["day"], p["count"] = day, 0
    p["hours"] = [t for t in p["hours"] if now - t < 3600]
    if p["count"] >= MAX_POSTS_DAY or state.get("limit_day") == day:
        QUEUE.clear()
        return
    if len(p["hours"]) >= MAX_POSTS_HOUR:
        return
    QUEUE.sort(key=lambda x: (x["prio"], x["ts"]))
    item = QUEUE.pop(0)
    if now - item["ts"] > item["ttl"]:
        return
    img = None
    try:
        img = make_card(item["base"], item["pos"], item["label"], item["metric"],
                        os.path.join(CARD_DIR, f"{item['base']}_{int(now)}.png"))
    except Exception as e:
        note_image_error(f"card creation failed: {e}")
    ok, info = post_square(item["text"], img)
    if ok:
        p["count"] += 1
        p["hours"].append(now)
        log(f"posted ${item['base']} ({info}) today={p['count']}")
    else:
        log(f"post failed ${item['base']}: {info}")
        if info == "LIMIT":
            state["limit_day"] = day
            tg("Binance Square daily post limit reached. Posting paused until next UTC day.")
    NEXT_POST = now + random.uniform(*POST_GAP)
    if img and not DRY_RUN:
        try:
            os.remove(img)
        except Exception:
            pass


# ----------------------------------------------------------------- state
def new_state():
    return {"mcaps": {}, "mcaps_ts": 0, "cool": {}, "open": [], "stats": {},
            "posts": {"day": "", "count": 0, "hours": []}, "limit_day": "", "summary_day": ""}


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        for k, v in new_state().items():
            s.setdefault(k, v)
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
    tbw = sum(k.get("tb", 0.0) for k in win)
    return {"chg": chg, "mult": mult, "vw": vw, "buy": (tbw / vw) if vw > 0 and tbw > 0 else None}


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
        if up and (not btc_state(now)["calm"] or (info["buy"] is not None and info["buy"] < 0.5)):
            STATS["flash:rejected (BTC dumping or sellers dominate)"] += 1
            continue
        t = tick[sym]
        cool_set(state, f"flash:{sym}", now)
        names = ["Volume"] + (["Buyers"] if info["buy"] is not None else []) + ["BTC"]
        post = compose("flash", t["base"], up,
                       {"chg": chg, "w": w, "mult": mult, "ratio": t["ratio"], "checks": names})
        enqueue(2, 600, t["base"], up, post,
                f"FLASH {'SPIKE' if up else 'DROP'} | {w}m",
                f"{chg:+.1f}% in {w}m   |   {mult:.1f}x volume",
                f"FLASH {t['base']}/USDT {chg:+.1f}% in {w}m, volume {mult:.1f}x, price {fmt(t['price'])}")
        log(f"flash {sym} {chg:+.1f}% {w}m vol {mult:.1f}x")


# ----------------------------------------------------------------- confluence: all checks before a signal post
STATS = defaultdict(int)
BTC = {"ts": 0.0, "ok": True, "calm": True, "chg1h": 0.0}
BTC_TF = {}
FNG = {"ts": 0.0, "val": None}


def sma(vals, n):
    return mean(vals[-n:]) if len(vals) >= n else None


def btc_state(now):
    """BTC position: ok = not dumping and not under its 15m EMA21; calm = not crashing. Cached 60s."""
    if now - BTC["ts"] < 60:
        return BTC
    BTC["ts"] = now
    kl = klines("BTCUSDT", "15m", 60)
    if kl and len(kl) > 30:
        c = [k["c"] for k in closed(kl)]
        BTC["chg1h"] = (c[-1] / c[-5] - 1) * 100
        BTC["calm"] = BTC["chg1h"] > -2.0
        BTC["ok"] = BTC["chg1h"] >= -BTC_MAX_DROP and c[-1] >= ema_series(c, 21)[-1] * 0.995
    return BTC


def btc_tf_ret(tf, now):
    """BTC return over the last 12 candles of this timeframe (cached 60s)."""
    ts, val = BTC_TF.get(tf, (0.0, None))
    if now - ts > 60:
        kl = fetch_tf("BTCUSDT", tf)
        val = (kl[-1]["c"] / kl[-13]["c"] - 1) if kl and len(kl) > 14 else None
        BTC_TF[tf] = (now, val)
    return val


def fng_state(now):
    """Crypto Fear & Greed index 0-100 (alternative.me, free). None if unavailable."""
    if now - FNG["ts"] > 3600:
        FNG["ts"] = now
        try:
            r = S.get("https://api.alternative.me/fng/?limit=1", timeout=15)
            FNG["val"] = int(r.json()["data"][0]["value"])
        except Exception:
            pass
    return FNG["val"]


def book_ratio(sym):
    """Bid depth / ask depth within 1% of the best price. None if unavailable."""
    d = bget("/api/v3/depth", {"symbol": sym, "limit": 100})
    try:
        bids = [(float(p), float(q)) for p, q in d["bids"]]
        asks = [(float(p), float(q)) for p, q in d["asks"]]
        bv = sum(p * q for p, q in bids if p >= bids[0][0] * 0.99)
        av = sum(p * q for p, q in asks if p <= asks[0][0] * 1.01)
        return bv / av if av > 0 else None
    except Exception:
        return None


def fib_check(h, l, c, a):
    """Bullish Fibonacci: price is at/near the swing high (breakout), or it holds above the 50%
    retracement of the last up-move without ever dipping below the 61.8% level."""
    n = min(len(c), 80)
    hh, ll = h[-n:], l[-n:]
    lo = min(ll)
    i = ll.index(lo)
    if i >= n - 5:
        return False
    hi = max(hh[i:])
    if hi - lo < 2 * a:
        return False
    if c[-1] >= hi - 0.5 * a:
        return True
    l50, l618 = hi - 0.5 * (hi - lo), hi - 0.618 * (hi - lo)
    return c[-1] >= l50 and min(ll[-8:]) >= l618


def fvg_check(h, l, c, a, look=24):
    """Bullish Fair Value Gap still unfilled under price (support), and no unfilled bearish gap
    sitting just above price (resistance)."""
    n = len(c)
    support = block = False
    for i in range(max(2, n - look), n):
        if l[i] > h[i - 2] and (l[i] - h[i - 2]) >= 0.2 * a:
            gb, gt = h[i - 2], l[i]
            later = l[i + 1:]
            if (min(later) if later else gt) > gb and c[-1] > gt:
                support = True
        if h[i] < l[i - 2] and (l[i - 2] - h[i]) >= 0.2 * a:
            gb, gt = h[i], l[i - 2]
            later = h[i + 1:]
            if (max(later) if later else gb) < gt and 0 < gt - c[-1] < a:
                block = True
    return support and not block


def confluence(kl, tf, mode, sym, now):
    """All checks on one candidate. Core checks must all pass, plus MIN_CONF of the extra ones."""
    h = [k["h"] for k in kl]
    l = [k["l"] for k in kl]
    c = [k["c"] for k in kl]
    qv = [k["qv"] for k in kl]
    a = atr_last(h, l, c) or 0.0
    passed, failed = [], []

    def mark(name, cond):
        if cond is not None:
            (passed if cond else failed).append(name)

    # core: EMA trend, RSI, volume, BTC not dumping
    r = rsi_last(c)
    if mode == "scalp":
        e9, e21 = ema_series(c, 9)[-1], ema_series(c, 21)[-1]
        ema_ok = c[-1] > e21 and e9 > e21
        rsi_ok = 50 <= r <= 80
        base = mean(qv[-21:-1])
        vol_ok = base > 0 and qv[-1] >= 1.8 * base
    else:
        e20, e55 = ema_series(c, 20)[-1], ema_series(c, 55)[-1]
        ema_ok = c[-1] > e20 and e20 > e55
        rsi_ok = 40 <= r <= 70
        base = mean(qv[-25:-5])
        vol_ok = base > 0 and mean(qv[-5:]) >= 1.4 * base
    bs = btc_state(now)
    core = [("EMA", ema_ok), ("RSI", rsi_ok), ("Volume", vol_ok), ("BTC", bs["ok"])]
    for name, cond in core:
        mark(name, cond)
    core_ok = all(cond for _, cond in core)

    # extra: MA, Fibonacci, FVG, buyer pressure, strength vs BTC, sentiment, order book
    s50 = sma(c, 50)
    s50_prev = mean(c[-55:-5]) if len(c) >= 55 else None
    extra = [("MA", s50 is not None and c[-1] > s50 and (s50_prev is None or s50 >= s50_prev)),
             ("Fib", fib_check(h, l, c, a)),
             ("FVG", fvg_check(h, l, c, a))]
    last3 = kl[-3:]
    tb_sum, q_sum = sum(k.get("tb", 0.0) for k in last3), sum(k["qv"] for k in last3)
    if q_sum > 0 and tb_sum > 0:
        extra.append(("Buyers", tb_sum / q_sum >= 0.52))
    brt = btc_tf_ret(tf, now)
    if brt is not None and len(c) > 14:
        extra.append(("vs BTC", (c[-1] / c[-13] - 1) > brt))
    fng = fng_state(now)
    if fng is not None:
        extra.append(("Sentiment", 20 <= fng <= 88))
    have = sum(1 for _, cond in extra if cond)
    if core_ok and have >= MIN_CONF - 1:              # order book only for promising candidates
        br = book_ratio(sym)
        if br is not None:
            extra.append(("Order book", br >= 0.8))
    for name, cond in extra:
        mark(name, cond)
    total = sum(1 for _, cond in extra if cond)
    return {"ok": core_ok and total >= MIN_CONF, "passed": passed, "failed": failed, "fng": fng}


def conf_line(conf):
    s = "\nChecks passed: " + ", ".join(conf["passed"])
    if conf["failed"]:
        s += "\nNot confirmed: " + ", ".join(conf["failed"])
    extra = []
    if conf.get("fng") is not None:
        extra.append(f"Fear&Greed {conf['fng']}")
    extra.append(f"BTC 1h {BTC['chg1h']:+.2f}%")
    return s + "\n" + " | ".join(extra)


def note_fail(mode, conf):
    STATS[f"{mode}:rejected"] += 1
    for name in conf["failed"]:
        STATS[f"{mode}:no_{name}"] += 1


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
    if not (trend and (brk or strong) and vm >= 1.8 and 50 <= r <= 80 and ext <= 3.5
            and tp_pct >= MIN_TP_PCT):
        return None
    return {"entry": c[-1], "atr": a, "vm": vm, "rsi": r, "brk": brk}


def scalp_scan(state, tick, tf, now):
    for sym in top_by_ratio(tick, SCALP_TOP):
        if not cool_ok(state, f"scalp:{sym}", SCALP_CD, now):
            continue
        kl = fetch_tf(sym, tf)
        sig = eval_scalp(kl)
        time.sleep(0.1)
        if not sig:
            continue
        STATS["scalp:setup"] += 1
        bk = bget("/api/v3/ticker/bookTicker", {"symbol": sym})
        try:
            spread = (float(bk["askPrice"]) / float(bk["bidPrice"]) - 1) * 100
        except Exception:
            continue
        if spread > MAX_SPREAD_PCT:
            STATS["scalp:rejected (spread)"] += 1
            continue
        conf = confluence(kl, tf, "scalp", sym, now)
        if not conf["ok"]:
            note_fail("scalp", conf)
            continue
        t = tick[sym]
        e, a = t["price"], sig["atr"]
        sl, tp1, tp2 = e - 1.5 * a, e + 1.5 * a, e + 3 * a
        risk_pct = (e - sl) / e * 100
        rr1 = (tp1 - e) / (e - sl) if (e - sl) > 0 else 0
        cool_set(state, f"scalp:{sym}", now)
        why = "breakout above recent highs" if sig["brk"] else "strong volume candle"
        post = compose("scalp", t["base"], True,
                       {"tf": tf, "why": why, "vm": sig["vm"], "ratio": t["ratio"],
                        "checks": conf["passed"]})
        msg = (f"SCALP {tf} {t['base']}/USDT\n"
               f"Entry: {fmt(e)}\n"
               f"SL: {fmt(sl)} ({(sl/e-1)*100:.2f}%)\n"
               f"TP1: {fmt(tp1)} (+{(tp1/e-1)*100:.2f}%)\n"
               f"TP2: {fmt(tp2)} (+{(tp2/e-1)*100:.2f}%)\n"
               f"Risk: {risk_pct:.2f}% | RR (TP1): 1:{rr1:.1f}\n"
               f"Spread: {spread:.3f}% | Vol: {sig['vm']:.1f}x | RSI: {sig['rsi']:.0f}")
        msg += conf_line(conf)
        state["open"].append({"sym": sym, "base": t["base"], "mode": "scalp", "tf": tf, "ts": now,
                              "entry": e, "sl": sl, "tp1": tp1, "ttl": 6 * 3600})
        enqueue(1, 1200, t["base"], True, post, f"SCALP SIGNAL | {tf}",
                f"Volume {sig['vm']:.1f}x   |   RSI {sig['rsi']:.0f}", msg)
        log(f"scalp {sym} {tf}")


# ----------------------------------------------------------------- SWING (new conditions)
def eval_swing(kl, tf, btc_ret):
    """
    New swing conditions (min 1h):
    - Price > EMA20 and EMA20 > EMA55
    - Price above EMA50 (or recent bounce)
    - RSI 40-70
    - Volume building (>= 1.4x)
    - Higher low structure
    - Bullish price action (close near high or strong body)
    """
    if not kl or len(kl) < 60:
        return None
    h = [k["h"] for k in kl]
    l = [k["l"] for k in kl]
    c = [k["c"] for k in kl]
    o = [k["o"] for k in kl]
    qv = [k["qv"] for k in kl]

    a = atr_last(h, l, c)
    if not a:
        return None

    ema20 = ema_series(c, 20)[-1]
    ema55 = ema_series(c, 55)[-1]
    ema50 = ema_series(c, 50)[-1]

    # Trend filters
    if not (c[-1] > ema20 and ema20 > ema55):
        return None
    if c[-1] < ema50 * 0.995:  # allow tiny undercut
        return None

    r = rsi_last(c)
    if not (40 <= r <= 70):
        return None

    base_vol = mean(qv[-25:-5])
    if base_vol <= 0:
        return None
    vr = mean(qv[-5:]) / base_vol
    if vr < 1.4:
        return None

    # Higher low
    higher_low = min(l[-5:]) > min(l[-15:-5])
    if not higher_low:
        return None

    # Bullish price action
    last = kl[-1]
    rng = last["h"] - last["l"]
    body = abs(last["c"] - last["o"])
    close_near_high = (last["h"] - last["c"]) / rng < 0.35 if rng > 0 else False
    strong_body = body / rng >= 0.55 if rng > 0 else False
    bullish_pa = close_near_high or strong_body or (last["c"] > last["o"])
    if not bullish_pa:
        return None

    # Relative strength vs BTC (optional soft filter)
    ret = c[-1] / c[-25] - 1 if len(c) > 25 else 0
    if btc_ret is not None and ret < btc_ret - 0.02:
        return None

    return {"entry": c[-1], "atr": a, "vr": vr, "rsi": r}


def swing_scan(state, tick, tf, now):
    btc = fetch_tf("BTCUSDT", tf)
    btc_ret = (btc[-1]["c"] / btc[-25]["c"] - 1) if btc and len(btc) > 30 else None
    for sym in top_by_ratio(tick, SWING_TOP):
        if not cool_ok(state, f"swing:{sym}", SWING_CD, now):
            continue
        kl = fetch_tf(sym, tf)
        sig = eval_swing(kl, tf, btc_ret)
        time.sleep(0.1)
        if not sig:
            continue
        STATS["swing:setup"] += 1
        conf = confluence(kl, tf, "swing", sym, now)
        if not conf["ok"]:
            note_fail("swing", conf)
            continue
        t = tick[sym]
        e, a = t["price"], sig["atr"]
        sl = e - 2.0 * a
        tp1 = e + 3.0 * a
        tp2 = e + 6.0 * a
        risk_pct = (e - sl) / e * 100
        rr1 = (tp1 - e) / (e - sl) if (e - sl) > 0 else 0
        cool_set(state, f"swing:{sym}", now)
        post = compose("swing", t["base"], True, {"tf": tf, "vr": sig["vr"], "ratio": t["ratio"],
                                                  "checks": conf["passed"]})
        msg = (f"SWING {tf} {t['base']}/USDT\n"
               f"Entry: {fmt(e)}\n"
               f"SL: {fmt(sl)} ({(sl/e-1)*100:.2f}%)\n"
               f"TP1: {fmt(tp1)} (+{(tp1/e-1)*100:.2f}%)\n"
               f"TP2: {fmt(tp2)} (+{(tp2/e-1)*100:.2f}%)\n"
               f"Risk: {risk_pct:.2f}% | RR (TP1): 1:{rr1:.1f}\n"
               f"Vol build: {sig['vr']:.1f}x | RSI: {sig['rsi']:.0f}\n"
               f"Structure: Higher lows + EMA alignment")
        msg += conf_line(conf)
        state["open"].append({"sym": sym, "base": t["base"], "mode": "swing", "tf": tf, "ts": now,
                              "entry": e, "sl": sl, "tp1": tp1, "ttl": 7 * 86400})
        enqueue(1, 3600, t["base"], True, post, f"SWING SIGNAL | {tf}",
                f"Volume building {sig['vr']:.1f}x   |   Higher lows", msg)
        log(f"swing {sym} {tf}")


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
        tg(f"RESULT {s['mode']} {s['tf']} {s['base']}: {res}")
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
        tg("\n".join(lines))


# ----------------------------------------------------------------- main loop
def main():
    state = load_state()
    log(f"start dry_run={DRY_RUN} poll={POLL_SEC}s runtime={MAX_RUNTIME_MIN}m "
        f"key={'yes' if square_key() else 'NO'} square_script={'yes' if find_square_script() else 'no (text-only posts)'}")
    tg(f"Coin bot started{' (dry run)' if DRY_RUN else ''}.")
    chk = ["node=" + (shutil.which("node") or "MISSING")]
    try:
        import PIL
        chk.append("Pillow=" + PIL.__version__)
    except Exception as e:
        chk.append(f"Pillow=MISSING({e})")
    try:
        make_card("TEST", True, "SELF CHECK", "image ok", os.path.join(CARD_DIR, "selfcheck.png"))
        os.remove(os.path.join(CARD_DIR, "selfcheck.png"))
        chk.append("card=ok")
    except Exception as e:
        chk.append(f"card=FAILED({e})")
    log("image check:", ", ".join(chk))
    if any(w in " ".join(chk) for w in ("MISSING", "FAILED")) or not find_square_script():
        tg("Image check problem: " + ", ".join(chk) + (" | post-image.mjs not found" if not find_square_script() else ""))
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
                flash_scan(state, tick, now)
                track(state, tick, now)
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
            drain(state, time.time())
            daily_summary(state, now)
            if now - last_prune > 3600:
                last_prune = now
                state["cool"] = {k: v for k, v in state["cool"].items() if now - v < 86400}
                if STATS:
                    log("filter stats (last hour):", dict(STATS))
                    STATS.clear()
            if now - last_save > 60:
                last_save = now
                save_state(state)
        except Exception:
            log("loop error:\n" + traceback.format_exc())
        while time.time() - t0 < POLL_SEC and time.time() < end:
            time.sleep(1)
            try:
                drain(state, time.time())
            except Exception:
                log("drain error:\n" + traceback.format_exc())
    save_state(state)
    log("finished runtime, state saved")


if __name__ == "__main__":
    main()
