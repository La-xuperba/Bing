#!/usr/bin/env python3
"""
Binance SPOT coin bot -> Binance Square.

Modes (one scanner):
  FLASH  5/10/15/30m  sudden price + volume spike  -> alert post
  SCALP  10/15/30m    breakout setups               -> signal post (+ levels on Telegram)
  SWING  1h/4h/1d     accumulation setups           -> signal post (+ levels on Telegram)

"Which coins to look at" = ranked by 24h volume as % of market cap (CoinGecko, free).
No price / volume / market-cap limit is applied to the universe.

Square post = max 4 lines, $COIN, English, mood, reason, 2 random hashtags, attention card image.
Posts are sent one by one with an 8-15 second gap. No results are posted to Square.
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

FLASH_CD = envf("FLASH_COOLDOWN_MIN", 45)
SCALP_CD = envf("SCALP_COOLDOWN_MIN", 60)
SWING_CD = envf("SWING_COOLDOWN_MIN", 720)

POST_GAP = (8.0, 15.0)
MAX_POSTS_DAY = int(envf("MAX_POSTS_DAY", 95))    # Square OpenAPI limit is 100/day
MAX_POSTS_HOUR = int(envf("MAX_POSTS_HOUR", 10))

HOSTS = ["https://data-api.binance.vision", "https://api.binance.com",
         "https://api1.binance.com", "https://api2.binance.com"]
GECKO = "https://api.coingecko.com/api/v3/coins/markets"
SQUARE_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"

STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDD", "EUR", "GBP", "TRY",
           "BRL", "AEUR", "USD1", "XUSD", "PYUSD", "UST", "USTC", "RLUSD", "BFUSD", "USDE"}

TAGS_NEUTRAL = ["#spottrading", "#signal", "#Spot", "#SpotTradingSuccess",
                "#TradingSignal", "#TradingTips", "#SignalAlert"]
TAGS_BULLISH = ["#SPOTCALL🔥🔥🔥", "#BullishMomentum"]

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
        if not base or base in STABLES:
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
    return f"${base} {mood}\n{headline}\n{' '.join(pick_tags(positive))}"


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
    """-> (ok, info). info == 'LIMIT' when the daily limit is hit."""
    if DRY_RUN:
        print("---- SQUARE POST (dry run) ----\n" + text + f"\n[image: {img}]\n")
        return True, "dry"
    key = square_key()
    if not key:
        return False, "no BINANCE_SQUARE_OPENAPI_KEY"
    env = dict(os.environ, BINANCE_SQUARE_OPENAPI_KEY=key)
    script = find_square_script()
    if script and img and os.path.exists(img):
        try:
            p = subprocess.run(["node", script, "--text", text, "--images", os.path.abspath(img)],
                               capture_output=True, text=True, timeout=120, env=env,
                               cwd=os.path.dirname(os.path.dirname(script)))
            out = (p.stdout or "") + (p.stderr or "")
            if p.returncode == 0 or "Success" in out:
                return True, "image"
            if "220009" in out:
                return False, "LIMIT"
            log("image post failed, trying text only:", out[-200:].replace("\n", " "))
        except Exception as e:
            log("image post error, trying text only:", e)
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
        QUEUE.pop()                                   # drop least important
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
        return                                        # too old, skip
    img = None
    try:
        img = make_card(item["base"], item["pos"], item["label"], item["metric"],
                        os.path.join(CARD_DIR, f"{item['base']}_{int(now)}.png"))
    except Exception as e:
        log("card error (posting text only):", e)
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
    if not (trend and (brk or strong) and vm >= 1.8 and 50 <= r <= 80 and ext <= 3.5
            and tp_pct >= MIN_TP_PCT):
        return None
    return {"entry": c[-1], "atr": a, "vm": vm, "rsi": r, "brk": brk}


def scalp_scan(state, tick, tf, now):
    for sym in top_by_ratio(tick, SCALP_TOP):
        if not cool_ok(state, f"scalp:{sym}", SCALP_CD, now):
            continue
        sig = eval_scalp(fetch_tf(sym, tf))
        time.sleep(0.1)
        if not sig:
            continue
        bk = bget("/api/v3/ticker/bookTicker", {"symbol": sym})
        try:
            spread = (float(bk["askPrice"]) / float(bk["bidPrice"]) - 1) * 100
        except Exception:
            continue
        if spread > MAX_SPREAD_PCT:
            continue
        t = tick[sym]
        e, a = t["price"], sig["atr"]
        sl, tp1, tp2 = e - 1.5 * a, e + 1.5 * a, e + 3 * a
        cool_set(state, f"scalp:{sym}", now)
        why = "breakout above recent highs" if sig["brk"] else "strong volume candle"
        head = f"Scalp setup ({tf}): {why}, volume {sig['vm']:.1f}x, uptrend intact."
        if t["ratio"] is not None:
            head += f" Vol/MCap {t['ratio']:.0f}%."
        msg = (f"SCALP {tf} {t['base']}/USDT\nEntry: {fmt(e)}\nSL: {fmt(sl)} ({(sl/e-1)*100:.2f}%)\n"
               f"TP1: {fmt(tp1)} (+{(tp1/e-1)*100:.2f}%)\nTP2: {fmt(tp2)} (+{(tp2/e-1)*100:.2f}%)\n"
               f"Spread {spread:.3f}%")
        state["open"].append({"sym": sym, "base": t["base"], "mode": "scalp", "tf": tf, "ts": now,
                              "entry": e, "sl": sl, "tp1": tp1, "ttl": 6 * 3600})
        enqueue(1, 1200, t["base"], True, compose(t["base"], True, head), f"SCALP SIGNAL | {tf}",
                f"Volume {sig['vm']:.1f}x   |   RSI {sig['rsi']:.0f}", msg)
        log(f"scalp {sym} {tf}")


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
    if not (vr >= 1.5 and flat and higher_low and above and near_high and rs and r <= 70):
        return None
    return {"entry": c[-1], "atr": a, "vr": vr, "rsi": r}


def swing_scan(state, tick, tf, now):
    btc = fetch_tf("BTCUSDT", tf)
    btc_ret = (btc[-1]["c"] / btc[-25]["c"] - 1) if btc and len(btc) > 30 else None
    for sym in top_by_ratio(tick, SWING_TOP):
        if not cool_ok(state, f"swing:{sym}", SWING_CD, now):
            continue
        sig = eval_swing(fetch_tf(sym, tf), tf, btc_ret)
        time.sleep(0.1)
        if not sig:
            continue
        t = tick[sym]
        e, a = t["price"], sig["atr"]
        sl, tp1, tp2 = e - 2 * a, e + 3 * a, e + 6 * a
        cool_set(state, f"swing:{sym}", now)
        head = (f"Swing setup ({tf}): volume building {sig['vr']:.1f}x while price holds a tight range, "
                f"higher lows, stronger than BTC.")
        msg = (f"SWING {tf} {t['base']}/USDT\nEntry: {fmt(e)}\nSL: {fmt(sl)} ({(sl/e-1)*100:.2f}%)\n"
               f"TP1: {fmt(tp1)} (+{(tp1/e-1)*100:.2f}%)\nTP2: {fmt(tp2)} (+{(tp2/e-1)*100:.2f}%)")
        state["open"].append({"sym": sym, "base": t["base"], "mode": "swing", "tf": tf, "ts": now,
                              "entry": e, "sl": sl, "tp1": tp1, "ttl": 7 * 86400})
        enqueue(1, 3600, t["base"], True, compose(t["base"], True, head), f"SWING SIGNAL | {tf}",
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
            if now - last_save > 60:
                last_save = now
                save_state(state)
        except Exception:
            log("loop error:\n" + traceback.format_exc())
        # keep draining the post queue while waiting for the next poll
        while time.time() - t0 < POLL_SEC and time.time() < end:
            time.sleep(1)
            try:
                drain(state, time.time())
            except Exception:
                log("drain error:\n" + traceback.format_exc())
    save_state(state)
    log("finished runtime, state saved")


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
    global klines, FLASH_COOLDOWN
    rnd = random.Random(7)
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    # indicators
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
    check("post has exactly 2 hashtags from the list",
          len(tags) == 2 and all(t in TAGS_NEUTRAL + TAGS_BULLISH for t in tags))
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
        enqueue(1, 600, b, True, compose(b, True, "test"), "TEST", "m")
    st = new_state()
    t0 = time.time()
    drain(st, t0)
    gap1 = NEXT_POST - t0
    drain(st, t0 + 1)                      # too early -> must not post
    check("8-15s gap enforced", 8 <= gap1 <= 15 and len(QUEUE) == 1)
    drain(st, NEXT_POST + 0.1)
    check("second post sent after gap", len(QUEUE) == 0 and st["posts"]["count"] == 2)
    print("\nALL OK" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
