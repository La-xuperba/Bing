"""Persistent state: cooldown de-duplication, signal serial, pending post queue.

One JSON file (default state/state.json). Writes are atomic (tmp + os.replace).
Signal de-duplication and flash de-duplication use SEPARATE dictionaries.
"""
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("binance_gems.state")

DEFAULT_STATE = {
    "version": 1,
    "signal_serial": 0,
    "last_signal_scan_ts": 0.0,
    "signal_cooldowns": {},   # "BTCUSDT" -> ts of last queued signal
    "flash_cooldowns": {},    # "Binance|ABCUSDT|15m" -> ts of last queued alert
    "queue": [],              # pending post items
    "recent_ids": [],         # ids already fully published (safety net)
    "last_template": {},      # kind -> last used wording index
}


def load_state(path):
    p = Path(path)
    state = json.loads(json.dumps(DEFAULT_STATE))
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state.update(data)
        except (OSError, ValueError) as exc:
            # Keep the broken file for inspection instead of silently resetting.
            backup = p.with_suffix(f".corrupt-{int(time.time())}")
            try:
                p.rename(backup)
            except OSError:
                pass
            log.error("State file unreadable (%s); moved to %s and starting fresh", exc, backup)
    return state


def save_state(state, path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, p)


def prune(state, now, flash_cd_s, signal_cd_s):
    for key, cd in (("flash_cooldowns", flash_cd_s), ("signal_cooldowns", signal_cd_s)):
        state[key] = {k: v for k, v in state[key].items() if now - v < cd * 2}
    state["recent_ids"] = state["recent_ids"][-1000:]


def in_cooldown(cooldowns, key, now, cooldown_s):
    ts = cooldowns.get(key)
    return ts is not None and (now - ts) < cooldown_s


def flash_keys(alert):
    return [f"{alert['exchange']}|{alert['symbol']}|{w}m" for w in alert["windows"]]


def _new_channels(telegram_on, square_on):
    return {"telegram": "pending" if telegram_on else "skipped",
            "square": "pending" if square_on else "skipped"}


def plan_flash_item(alert, state, now, cooldown_s):
    """Return (item|None). Dedup per exchange/pair/timeframe; one post per pair per scan.

    Only timeframes that are NOT in cooldown count; the post is created if at
    least one fresh timeframe remains. All triggered timeframes are put in
    cooldown so the same pump is not re-posted by a neighbouring window.
    """
    fresh = [w for w in alert["windows"]
             if not in_cooldown(state["flash_cooldowns"], f"{alert['exchange']}|{alert['symbol']}|{w}m", now, cooldown_s)]
    if not fresh:
        return None
    for k in flash_keys(alert):
        state["flash_cooldowns"][k] = now
    item_id = f"flash:{alert['exchange']}:{alert['symbol']}:{int(alert['window_end_ms'] // 60000)}"
    return {"id": item_id, "kind": "flash", "created_ts": now, "data": alert, "text": None}


def plan_signal_item(row, state, now, cooldown_s):
    """Regular signal item for an ELIGIBLE scanner row (a plain dict)."""
    sym = row["symbol"]
    if in_cooldown(state["signal_cooldowns"], sym, now, cooldown_s):
        return None
    state["signal_cooldowns"][sym] = now
    state["signal_serial"] += 1
    return {"id": f"signal:{sym}:{row.get('candle_close_ms', int(now))}", "kind": "signal",
            "created_ts": now, "data": dict(row, serial=state["signal_serial"]), "text": None}


def enqueue(state, item, telegram_on, square_on):
    ids = {q["id"] for q in state["queue"]} | set(state["recent_ids"])
    if item["id"] in ids:
        return False
    item["channels"] = _new_channels(telegram_on, square_on)
    item["attempts"] = {"telegram": 0, "square": 0}
    state["queue"].append(item)
    return True


def item_done(item):
    return all(v not in ("pending",) for v in item["channels"].values())


def drop_expired(state, now, flash_max_age_s, signal_max_age_s):
    keep, dropped = [], []
    for it in state["queue"]:
        max_age = flash_max_age_s if it["kind"] == "flash" else signal_max_age_s
        (dropped if now - it["created_ts"] > max_age else keep).append(it)
    state["queue"] = keep
    return dropped


def ordered_queue(state):
    """Flash alerts are time-sensitive, so they go first; otherwise oldest first."""
    return sorted(state["queue"], key=lambda q: (0 if q["kind"] == "flash" else 1, q["created_ts"]))
