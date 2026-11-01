# BINANCE GEMS v2 — Early-Pump Signals + Flash Pump Alerts

Two **independent** systems, one shared publishing queue. The bot never places orders.

| | Early-pump **signal** | **Flash Pump Alert** |
|---|---|---|
| Purpose | Find promising coins *before* a big move | Report a move that is *already happening* |
| Logic | Your original scanner, unchanged | `flash_pump.py`: ≥ +5% in 5 / 15 / 30 min |
| Exchanges | Binance Spot USDT | Binance Spot + Gate.io Spot USDT |
| Score / serial | Yes (score + `#0001` serial) | **Never** |
| Filters | Original filters (price ≥ 0.01, 24h < 15%, score ≥ 45 …) | No price / market-cap / score filter |
| Dedup | per symbol, 120 min | per exchange + pair + timeframe, 60 min |

A coin can produce a signal, a flash alert, both, or nothing. A pumped coin that fails the
original signal rules gets **only** a flash alert; the signal rules are never relaxed.

---

## 1. What changed from your original bot

**Unchanged (strategy):** `binance_get`, `ema`, `rsi`, `atr`, `macd_histogram`, `get_klines`,
`build_universe`, `get_btc_return_6h`, `screen_symbol`, `scan` and every threshold constant
(`MIN_SCORE=45`, RSI 52–67, volume ratio 1.5 / 1.2, 24h change < 15%, min price 0.01 …).
`tests/test_separation_dedup.py` pins the constants and exercises `screen_symbol`.

**Added to `screen_symbol`'s returned dict (no effect on eligibility):** `trend_ok`, `breakout_ok`,
`momentum_ok` so post wording only states facts that were measured.

**Replaced because your prompt requires it:**
- One bulk Telegram message per scan → **one post per coin**, 10–20 s apart (`make_telegram_message`, `post_telegram` removed).
- Square text drafts that carried a chart → **text-only** posts, so chart creation (`create_chart`, `make_square_posts`, `save_square_drafts`, matplotlib) was removed.
- `SQUARE_POST_DELAY_SECONDS=45` → random `POST_DELAY_MIN/MAX_SECONDS` (10–20).
- `BINANCE_SQUARE_API_TOKEN` → `BINANCE_SQUARE_API_KEY`.

**Your original code had no** CoinGecko/CoinMarketCap logic and **no** signal serial numbering, so
there was nothing to preserve there. I did **not** invent market-cap thresholds. The two secrets are
passed to the workflow as `COINGECKO_API_KEY` / `COINMARKETCAP_API_KEY` but are unused. A persistent
signal serial (`#0001`, `#0002` …) is new, because your prompt refers to one.

**New (not in your original):** a per-symbol signal cooldown (120 min) so the same coin is not
re-posted every 15 minutes. Set `SIGNAL_COOLDOWN_MINUTES=0` to get the old repeat behaviour.

Your original files are untouched in your upload. This project is a separate folder.

## 2. Flash pump calculation (exact method)

Source: 1-minute candles with **exchange timestamps** (Binance klines / Gate.io candlesticks).

```
end_price    = close of the latest 1m candle   (this candle is usually still forming,
                                                 so it is the latest traded price - intentional)
window_start = open_time(latest candle) - (N-1) minutes
start_price  = open of the candle that starts at window_start
               (if that minute has no candle -> close of the last earlier candle, i.e. price carried
                forward because nothing traded; if nothing earlier exists -> window SKIPPED)
pct          = (end_price / start_price - 1) * 100       N = 5, 15, 30
alert when   round(pct, 6) >= FLASH_THRESHOLD_PCT (5.0)
```

The effective window is between N-1 and N minutes depending on seconds elapsed in the current minute.

A window is **skipped (never reported)** when: no candles, latest candle older than ~4 min vs. exchange
server time, price ≤ 0 / NaN / inf, new listing without enough history, or an implausible > 1000% jump.

**Efficiency (no thousands of requests):** a +5% rise within 30 min implies
`last ≥ 24h_low × 1.05`. Pairs failing this (checked from one bulk 24h-ticker call) cannot qualify,
so candles are downloaded only for the few candidates. Requests go through a per-exchange rate
limiter (`BINANCE_RPS`, `GATE_RPS`, default 12/s), a small worker pool, and retry/backoff on 418/429/5xx
with `Retry-After`. `GATE_MIN_QUOTE_VOLUME_24H` defaults to 0 (off).

## 3. GitHub Actions vs continuous monitoring — be realistic

This is a **periodic scanner, not real-time detection**.

| | Actions, every 5 min (this project) | Always-on process (WebSocket) |
|---|---|---|
| Latency | 0–5 min + queue + GitHub delay (scheduled runs are often late, sometimes skipped) | seconds |
| Short pumps | A spike that starts and fades between two runs can be **missed** | caught |
| Cost / ops | Free on public repos; private repos burn minutes (~288 runs/day) | needs a VPS or similar |
| Reliability | GitHub-managed; state in a git branch | you manage restarts |

Detection windows are rolling 5/15/30 min, so a pump that is *still elevated* when the next run starts
is still caught; a pump that fully reverses within ~5 min may not be. If you need seconds-level alerts,
run the same `flash_pump.py` logic on a small VPS fed by exchange WebSocket streams. That is not
included here.

## 4. Publishing, queue and state

- Detection and publishing are separate. Everything detected is written to `state.json` **before** the
  first post is sent.
- Posts go out one coin at a time; a random 10–20 s pause (`random.uniform`) is applied between posts,
  across signals and alerts (flash alerts first, they are time-sensitive). No pause after the last post.
- `BOT_MAX_RUNTIME_SECONDS` (600) is a soft budget. When it runs out, unsent posts **stay in the queue**
  and are sent by the next run. State is saved after every channel result, so a crash cannot resend finished posts.
- Queued posts expire (`FLASH_MAX_AGE_MINUTES=45`, `SIGNAL_MAX_AGE_MINUTES=120`) so you never get a "flash alert" an hour late.
- State lives on a dedicated `bot-state` branch (single amended commit, force-pushed by the workflow, no history growth).
  The file contains market data and post text only, no secrets.
- Telegram: official Bot API, timeouts, exponential backoff, 429 `retry_after`, token redacted from logs/errors.
- Binance Square: see below.

### Binance Square — what is and is not verified

I could not open Binance's developer portal from my environment. The request format below comes from
the `square-post` skill of Binance's `binance/binance-skills-hub` repository, as shown on public mirrors
of it. **Please compare it with the current official docs before relying on it.**

```
POST https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add
X-Square-OpenAPI-Key: <key>   Content-Type: application/json   clienttype: binanceSkill
{"bodyTextOnly": "<text>"}      -> success: {"code":"000000","data":{"id":"..."}}
```

A post is counted as published **only** when HTTP 200, `code == "000000"` **and** a post id is returned.
Known error codes (220003/220004/220010/220011/2000001/2000002/30008) are permanent; `220009`
(daily OpenAPI limit) stops further Square attempts for the run. In every non-success case the text is
appended to `square_drafts.txt` (uploaded as a workflow artifact) with the reason. Without
`BINANCE_SQUARE_API_KEY` all Square posts become drafts. Create the key with minimal permissions (no trading/withdrawal).

## 5. Installation

1. Create a GitHub repository (**public** = free Actions minutes; private repos have a monthly minute quota and a 5-min schedule will exceed the free one). Upload the contents of this folder (keep `.github/workflows/binance-gems.yml` at that exact path). Leave your old files out of the repo or in a separate branch until you have validated this version.
2. **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN` — from @BotFather
   - `TELEGRAM_CHAT_ID` — channel/group id (the bot must be an admin of the channel)
   - `BINANCE_SQUARE_API_KEY` — optional (otherwise drafts)
   - `COINGECKO_API_KEY`, `COINMARKETCAP_API_KEY` — optional, currently unused
3. **Settings → Actions → General → Workflow permissions**: "Read and write permissions" (needed only to push the `bot-state` branch).
4. **Actions → BINANCE GEMS → Run workflow**. First try with `dry_run = true` (prints the posts, publishes nothing, saves nothing), then run without it.
5. The `*/5` schedule starts automatically. The early-pump scan itself runs every 15 minutes (`SIGNAL_SCAN_INTERVAL_MINUTES`); flash scans run every time.

> Binance's main API (`api.binance.com`) blocks US IP ranges, which GitHub runners use. As in your original, the bot uses `data-api.binance.vision` first (`BINANCE_BASE_URL`). If Gate.io or Binance is unreachable the bot logs it and continues with the other system.

### Local run
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in, then: set -a; source .env; set +a
python crypto_signal_bot.py --dry-run
python -m pytest -q
```

## 6. Tuning

See `.env.example` for every variable (thresholds, cooldowns, delays, exchanges, rate limits).
Do not set the delay range outside 10–20 s if you want to stay within your stated requirement.

## 7. Files

```
crypto_signal_bot.py   main bot: ORIGINAL scanner + orchestration + queue publisher
flash_pump.py          flash detection, Binance/Gate clients, calculation (documented)
state_store.py         state JSON, cooldown dedup, signal serial, queue helpers
post_copy.py           varied post wording (data-driven)
publishers.py          Telegram + Binance Square adapters, draft writer
http_util.py           rate limiter, retrying GET
tests/                 60 offline tests
.github/workflows/binance-gems.yml
requirements.txt  requirements-dev.txt  .env.example  README.md
```

## 8. Test status

**Run and passing (offline, 60 tests):** Python syntax; 5m/15m/30m +5% threshold (boundary 4.99 / 5.00);
missing candles, new listings, invalid prices, stale data; ineligible pumper → flash only; eligible coin →
signal independently of flash; original constants and `screen_symbol` eligibility on synthetic data;
no score/serial in flash posts; random 10–20 s delays, one coin per post, queue persistence on runtime
exhaustion, no repost after restart; Telegram 429/5xx/network retry and token redaction; Square success /
failure / draft fallback against mocked responses; workflow YAML structure; full `run()` twice with fakes.

**Not tested (needs live access or credentials):** real Binance/Gate.io responses, real Telegram
delivery, real Binance Square publishing, the GitHub Actions run itself (state branch push, schedule
timing). Run the workflow once with `dry_run` and once for real and check the logs.
