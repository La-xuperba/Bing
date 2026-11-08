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
