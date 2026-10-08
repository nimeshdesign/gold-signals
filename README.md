# Gold Signals

A private, rule-based XAUUSD (gold) signal tool for one trader:

- **Engine** (`app/engine.py`): watches gold while the market is open (Sun 6 PM – Fri 5 PM New York time), sends signals to your private Telegram channel, and follows each open trade minute by minute until TP1, TP2 or the stop.
- **Dashboard** (`app/web/`, behind a login): live status and open trades, a chart with every level, analytics, and the track record.
- **Backtester** (`scripts/backtest.py`): runs the same rules on 3 years of history.

```
Twelve Data ──► engine ──► SQLite ◄── dashboard (login)
                  │
                  └──► outbox ──► Telegram (signals, TP/SL updates, daily plan, alerts)
```

## The strategy: Asian-session breakout

| Part | Rule (set in `.env` as `STRATEGY`, `STRATEGY_PARAMS`, `TP1_R`, `TP2_R`) |
|---|---|
| Range | gold's high and low from 00:00 to 06:00 UTC |
| Entry | the first 15-min close outside the range before 16:00 UTC, at most one signal a day |
| Trend filter | buys only when the daily close is above its 20 EMA; sells only below |
| Stop loss | the range width, kept between 0.5× and 3× the 1-hour ATR |
| Targets | TP1 = 1R, TP2 = 3R. Half closes at TP1 and the stop moves to entry |
| News filter | no new signals from 30 min before to 30 min after high-impact USD events |
| Limits | 1 open signal at a time; anything still open after 48h closes at market |

Results are counted in R (1R = entry-to-stop distance): stop = −1R, TP1 then breakeven = +0.5R, TP1 then TP2 = +2R. When one candle touches both the stop and a target, the stop is counted.

### Backtest (Oct 2023 – Oct 2026, $0.30 spread)

| Year | Trades | Win rate | Net R |
|---|---|---|---|
| 2023 (Oct–Dec) | 41 | 61.0% | +8.8 |
| 2024 | 129 | 54.3% | +13.0 |
| 2025 | 122 | 55.7% | +22.7 |
| 2026 (to Oct) | 81 | 65.4% | +27.0 |
| **Total** | **373** | **57.9%** | **+71.5** |

Profit factor 1.47, max drawdown 11.3R, longest losing streak 6. Buys and sells are both profitable. With a $0.80 spread it is still +50R.

### Current live configuration (both setups, 100-pip stop)

Asian breakout + New York open breakout, SL 100 / TP1 100 / TP2 300 pips, max 2 open, $0.30 spread, weekends removed, stop gaps filled at the gap price, 48-hour expiry in market hours (`python -m scripts.backtest --csv data/xauusd_15min_2023-10-01_now.csv`):

886 trades, **54.3% wins, +74.0R** (profit factor 1.18), **max drawdown 23.0R**, longest losing streak 7. Asian breakout +54.3R, New York breakout +19.8R; BUYs +76.4R, SELLs −2.3R.

Tested and **not adopted** (no robust gain): one trade per direction (+40.4R), no Friday entries after 16:00 / 12:00 UTC (+72.6R / +66.2R), buy-only (+78.0R overall but −3.0R in 2026), stop as a % of price.

### Strategy research: what it proves and what it doesn't

`python -m scripts.research --csv data/xauusd_15min_2023-10-01_now.csv` tests 704 settings across four strategy types (`app/strategies.py`). It tunes on Nov 2023 – Dec 2025 and checks on 2026.

- The single best setting on training data (an RSI(2) pullback) **failed** on 2026: 36% win rate, −10R. When many settings are tried, the best one is usually best partly by luck.
- The session breakout was the only type that held up across nearly all its settings: 86% of its 128 settings were profitable in training and 100% in 2026. It was chosen for that consistency, not for one lucky setting.
- Because the 2026 data was used to choose it, no untouched test data is left. **Forward paper trading is the real test.** Run the engine with `DRY_RUN=false` into a private channel for 1–2 months before charging anyone, and expect live results to be worse than the backtest.

### Walk-forward test (stricter)

`python -m scripts.walkforward --csv data/xauusd_15min_2023-10-01_now.csv` picks the best setting on the previous 12 months, trades the next 3 months with it, rolls forward, and scores only those out-of-sample months (Oct 2024 – Oct 2026, $0.30 spread):

| Setting pool | Trades | Win rate | Net R | Profit factor | t-stat | Profitable 3-month periods |
|---|---|---|---|---|---|---|
| Breakout, no add-ons | 297 | 48.5% | +39.9 | 1.26 | 1.82 | 7/9 |
| + ADX trend strength | 274 | 46.4% | +44.3 | 1.30 | 1.99 | 7/9 (ADX chosen 1/9) |
| + London–New York overlap only | 316 | 50.0% | +34.8 | 1.22 | 1.60 | 6/9 |
| + previous-day high/low | 282 | 50.0% | +47.3 | 1.34 | 2.21 | 7/9 (filter chosen 5/9) |

The edge survives out of sample but is about a third smaller than the single backtest suggested, and it falls just short of statistical significance (t ≈ 1.96). None of the add-ons is a clear improvement, so the live strategy is unchanged. They stay available as optional `STRATEGY_PARAMS` (`adx_min`, `window_start`, `pd_filter`: `"beyond"` or `"room"`).

## 1. Local setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # Linux/macOS: cp .env.example .env
pytest                          # 25 tests
```

Get a free API key at [twelvedata.com](https://twelvedata.com) and put it in `.env` as `TWELVE_DATA_API_KEY`.

## 2. Backtest

```bash
python -m scripts.backtest --download --start 2023-10-01
```

This downloads about 3 years of 15-minute candles (a few minutes on the free plan, cached in `data/`; Twelve Data's 15-min gold history starts around Oct 2023), then prints the win rate, total R, profit factor, max drawdown, losing streaks and per-year results, and writes every trade to `backtest_trades.csv`. `--spread 0.30` sets the per-trade cost in $/oz; use your broker's typical gold spread.

You can also use your own data: `--csv file.csv` with columns `datetime,open,high,low,close` (UTC).

To test another strategy or setting: `--strategy trend_pullback --tp1 1.5 --tp2 3`, or `--params` with JSON overrides.

What to look for: a positive total **after costs** in most years, a profit factor clearly above 1.2, and a drawdown you could sit through. If a result only works in one year, it probably isn't an edge.

The backtest doesn't apply the news filter, because there's no free historical calendar, so live trading will skip some of these trades.

## 3. Telegram

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token to `TELEGRAM_BOT_TOKEN`.
2. Create a **private** channel. Add the bot as an admin with *Post messages*.
3. Run `python -m scripts.telegram_setup` to find the channel id, then `python -m scripts.telegram_setup --save <id>`.
4. Check everything with `python -m scripts.telegram_test` (`--quiet` to send nothing).

Run one cycle with `DRY_RUN=true` to see the messages in your terminal:

```bash
python -m app.engine --once
```

Then set `DRY_RUN=false` and run `python -m app.engine` to keep it running.

## 4. Dashboard login

`python -m scripts.set_password` stores a hashed password in `.env` (and signs every device out; add `--keep-sessions` to avoid that). Locally: `uvicorn app.web.main:app --reload`, then open http://localhost:8000.

## 5. Deploy on a server

The live setup runs on an Oracle Cloud "Always Free" Ubuntu VM. Upload the project folder and run:

```bash
bash deploy/setup_server.sh --web     # installs, runs the tests (stops if any fail), restarts the services
sudo journalctl -u gold-engine -f     # watch the engine
```

The script keeps the server's own `.env` and `data/`, runs the code as root-owned and read-only, and lets the services write only to `data/`. It also schedules a private daily backup (`deploy/backup.sh`); copy `backups/` off the server now and then. For HTTPS, put your hostname in a Caddyfile (see `deploy/Caddyfile`) and open ports 80/443 in the cloud firewall.

## Reliability

- **Weekends:** vendors publish flat weekend quotes; they are dropped everywhere (`app/market.py`), so no weekend signals, tracking or expiries.
- **Data allowance (800 requests/day):** trade tracking speed is set from what is left after reserving the remaining 15-minute checks; an "out of credits" reply pauses data requests until 00:00 UTC.
- **Missed checks:** a failed cycle is retried after a minute and catches up on candles that closed since the last good one (up to 30 minutes; later signals are marked "late").
- **Telegram:** every message goes through the `outbox` table in the same transaction as the change it reports, and is retried until sent.
- **Alerts:** repeated failures, the data limit and low data allowance post a ⚠️ message to the channel.

## Project layout

```
app/
  config.py        settings from .env
  indicators.py    EMA, RSI, ATR (Wilder, matches TradingView/MT5)
  strategies.py    strategy library: session_breakout (live), rsi2_reversion, trend_pullback, ema_cross
  strategy.py      original EMA-cross rules + interval helper
  simulator.py     backtest trade simulator with realistic spread
  outcome.py       TP1/TP2/SL/breakeven trade management (shared)
  data_feed.py     Twelve Data client, optional MT5 feed, CSV loader
  news.py          ForexFactory high-impact USD news filter
  telegram_bot.py  Bot API client + message templates
  engine.py        the 24/5 loop: signals, minute-by-minute trade tracking, daily messages, alerts
  market.py        gold market hours (weekend filter, market-time expiry)
  db.py            SQLite schema and queries (signals, candles, outbox, kv)
  web/             FastAPI dashboard: login, status, chart, analytics, track record
scripts/          backtest.py, research.py (strategy search), load_demo.py (preview data)
deploy/            systemd units, Caddyfile, backup script
tests/
```

## Known limits

- Backtests manage trades on 15-minute candles (live uses 1-minute). Within a single candle the real order of moves is unknown, so a stop is always counted first when both are touched. Stops gapped through fill at the gap price.
- Live results are scored like the tracker: SELL spread included (stops/targets checked at the ask), BUY spread not, no slippage. Your broker fills will differ slightly.
- The news feed covers the current week only. If it can't be fetched, signals continue by default; set `NEWS_FAIL_CLOSED=true` to pause signals instead.
- MT5 as a data source: `MT5Feed` in `data_feed.py` works on Windows with a running terminal (`pip install MetaTrader5`, and set `MT5_SERVER_UTC_OFFSET_HOURS`). It isn't wired to a setting yet; swap it in `Engine.__init__`.
