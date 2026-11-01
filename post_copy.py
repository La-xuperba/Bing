"""Post wording. Varied copy, but every claim comes from measured data.

Signals keep the original Telegram data lines (price / 24h / volume / RSI /
reasons). Flash alerts never contain a score or a signal serial number.
"""
import random

from flash_pump import WINDOWS_MIN  # noqa: F401  (documented dependency)


def fmt_price(value):
    value = float(value)
    if value >= 1000: return f"{value:,.2f}"
    if value >= 1: return f"{value:.4f}".rstrip("0").rstrip(".")
    if value >= 0.01: return f"{value:.5f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _pick(options, rng, last):
    """Pick an index from options, avoiding `last` when there is a choice."""
    idxs = [i for i in range(len(options)) if i != last] or [0]
    return rng.choice(idxs)


# ----------------------------------------------------------------------------- signals
_SIGNAL_INTROS = [
    # (predicate(d), template)
    (lambda d: d["volume_ratio_15m"] >= 2.5,
     "${c} just woke up on the 15m chart - volume is running {vol:.1f}x its recent average."),
    (lambda d: d.get("breakout_ok"),
     "${c} pushed through its recent 15m highs while the bigger picture still looks constructive."),
    (lambda d: d["relative_strength_vs_btc_pct"] >= 1,
     "While BTC does its thing, ${c} is quietly outperforming it ({rs:+.1f}% relative over 6h)."),
    (lambda d: d.get("trend_ok"),
     "Trend check on ${c}: the 1h structure is lined up and the scanner likes what it sees."),
    (lambda d: d.get("momentum_ok"),
     "${c} just cleared the momentum filter - RSI and MACD are leaning the right way."),
    (lambda d: True,
     "${c} made it through this scan's checks. Here is what stood out."),
    (lambda d: True,
     "Fresh on the radar: ${c}, with a score of {score}/100."),
]

_SIGNAL_CLOSERS = [
    "Early reads can fail - size your risk before acting.",
    "Watch only. Confirm your own entry and invalidation level.",
    "No guarantees here; markets reverse fast.",
    "Do your own checks on liquidity and risk first.",
    "Treat it as a watchlist idea, not a promise.",
]


def render_signal(d, rng=None, last=None):
    """d: eligible scanner row (dict). Returns (text, state_indexes)."""
    rng = rng or random.Random()
    last = last or {}
    usable = [i for i, (pred, _) in enumerate(_SIGNAL_INTROS) if pred(d)]
    # prefer specific intros over the two generic fallbacks when available
    specific = [i for i in usable if i < len(_SIGNAL_INTROS) - 2] or usable
    pool = [i for i in specific if i != last.get("signal_intro")] or specific
    i_intro = rng.choice(pool)
    i_close = _pick(_SIGNAL_CLOSERS, rng, last.get("signal_close"))
    intro = _SIGNAL_INTROS[i_intro][1].format(
        c=d["base_asset"], vol=d["volume_ratio_15m"], rs=d["relative_strength_vs_btc_pct"], score=d["score"])
    lines = [
        f"💎 BINANCE GEMS | PRE-PUMP RADAR  #{int(d['serial']):04d}",
        "",
        intro,
        "",
        f"${d['base_asset']} — Score {int(d['score'])}/100",
        f"💵 Price: {fmt_price(d['close'])} USDT",
        f"📈 24h: {d['price_change_24h_pct']:+.2f}% | Vol: ${d['quote_volume_24h'] / 1e6:.2f}M",
        f"⚡ 15m volume: {d['volume_ratio_15m']:.2f}x | RSI: {d['rsi_15m']:.1f}",
        f"🔎 {d['reasons']}",
        f"⚠️ {_SIGNAL_CLOSERS[i_close]}",
    ]
    return "\n".join(lines), {"signal_intro": i_intro, "signal_close": i_close}


# ----------------------------------------------------------------------------- flash
_FLASH_BODIES = [
    "${c} is moving sharply - up {pct:+.1f}% over the last {w} minutes.",
    "Heads up: ${c} just ripped {pct:+.1f}% in about {w} minutes.",
    "Sudden strength in ${c}. The price is up {pct:+.1f}% across the last {w} minutes.",
    "${c} is sprinting: {pct:+.1f}% inside a {w}-minute window.",
    "Big candle activity on ${c} - a {pct:+.1f}% jump in roughly {w} minutes.",
    "Price check on ${c}: {pct:+.1f}% higher than it was {w} minutes ago.",
]

_FLASH_WARNINGS = [
    "A rapid move is underway. Check liquidity and volatility before making any decision.",
    "Moves like this can reverse as quickly as they start - spreads and slippage may be wide.",
    "Chasing a fast move is risky. Look at order-book depth first.",
    "Thin books can exaggerate swings. Verify before you act.",
    "This is an informational alert only, not advice. Volatility is high right now.",
]


def render_flash(a, rng=None, last=None):
    """a: flash alert dict. No score, no signal serial - ever."""
    rng = rng or random.Random()
    last = last or {}
    i_body = _pick(_FLASH_BODIES, rng, last.get("flash_body"))
    i_warn = _pick(_FLASH_WARNINGS, rng, last.get("flash_warn"))
    body = _FLASH_BODIES[i_body].format(c=a["base"], pct=a["primary_pct"], w=a["primary_window"])
    lines = [
        f"⚡ FLASH PUMP ALERT — {a['exchange']} Spot",
        "",
        body,
        "",
        f"Pair: {a['base']}/{a['quote']}",
        f"Price: {fmt_price(a['price'])} {a['quote']}",
        f"Change: {a['primary_pct']:+.2f}% / {a['primary_window']}m",
    ]
    others = sorted((int(w), p) for w, p in a["windows"].items() if int(w) != a["primary_window"])
    if others:
        lines.append("Also: " + ", ".join(f"{p:+.2f}% / {w}m" for w, p in others))
    lines += [f"Detected: {a['detected_at']}", "", i_warn_text(i_warn)]
    return "\n".join(lines), {"flash_body": i_body, "flash_warn": i_warn}


def i_warn_text(i):
    return _FLASH_WARNINGS[i]
