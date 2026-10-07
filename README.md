# Gold Signals

A rule-based XAUUSD signal service:

- **Engine** (`app/engine.py`): watches gold 24/5, sends signals to a private Telegram channel, and follows up on each trade when it hits TP1, TP2 or the stop.
- **Website** (`app/web/`): landing page, a public track record with every closed signal, pricing, and Stripe checkout.
- **Access control**: after payment, each customer gets a single-use Telegram link. The bot approves their join request and removes them when their subscription ends.
- **Backtester** (`scripts/backtest.py`): runs the same rules on 2–3 years of history.

```
Twelve Data ──► engine ──► SQLite ◄── website ◄── Stripe webhooks
                  │                      │
                  └──► Telegram ◄────────┘  (invite links, approvals, removals)
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

### Strategy research: what it proves and what it doesn't

`python -m scripts.research --csv data/xauusd_15min_2023-10-01_now.csv` tests 704 settings across four strategy types (`app/strategies.py`). It tunes on Nov 2023 – Dec 2025 and checks on 2026.

- The single best setting on training data (an RSI(2) pullback) **failed** on 2026: 36% win rate, −10R. When many settings are tried, the best one is usually best partly by luck.
- The session breakout was the only type that held up across nearly all its settings: 86% of its 128 settings were profitable in training and 100% in 2026. It was chosen for that consistency, not for one lucky setting.
- Because the 2026 data was used to choose it, no untouched test data is left. **Forward paper trading is the real test.** Run the engine with `DRY_RUN=false` into a private channel for 1–2 months before charging anyone, and expect live results to be worse than the backtest.

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
2. Create a **private** channel. Add the bot as an admin with *Invite users via link*, *Ban users* and *Post messages*.
3. Find the channel id: post a message in the channel, forward it to [@userinfobot](https://t.me/userinfobot) (or a similar bot), and put the `-100…` id in `TELEGRAM_CHANNEL_ID`.
4. Optional: put a public channel in `TELEGRAM_PUBLIC_CHANNEL_ID`. It receives only closed results, which works as free marketing.

Run one cycle with `DRY_RUN=true` to see the messages in your terminal:

```bash
python -m app.engine --once
```

Then set `DRY_RUN=false` and run `python -m app.engine` to keep it running.

## 4. Stripe

1. In the Stripe dashboard, create a Product with a **recurring** Price and put the price id in `STRIPE_PRICE_ID`. Put your secret key in `STRIPE_SECRET_KEY`. Use test-mode keys until launch.
2. Under Settings → Billing → Customer portal, turn on the portal so customers can cancel and update their card.
3. Under Settings → Public details, add a Terms of Service URL. Checkout requires customers to accept it.
4. Add a webhook endpoint at `https://yourdomain.com/stripe/webhook` with these events:
   `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`.
   Copy its signing secret to `STRIPE_WEBHOOK_SECRET`.

Local testing: `stripe listen --forward-to localhost:8000/stripe/webhook`, then pay with card `4242 4242 4242 4242`.

Website: `uvicorn app.web.main:app --reload`, then open http://localhost:8000.

**What a customer goes through:** Pricing → Stripe Checkout → `/success` shows their personal invite link → in Telegram they tap *Request to join* → the engine's bot approves them (the engine must be running) and revokes the link so it can't be shared. When the subscription is canceled or unpaid, the bot removes them from the channel.

## 5. Deploy on a VPS

Gold trades about 23 hours a day, 5 days a week, so run this on a server. A $6/month Ubuntu VPS (DigitalOcean, Hetzner, Contabo) is enough.

```bash
sudo adduser --system --group gold
sudo git clone <your repo> /opt/gold-signals     # or copy the folder up with scp
cd /opt/gold-signals
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env      # SITE_URL=https://yourdomain.com, DRY_RUN=false
sudo chown -R gold:gold /opt/gold-signals

sudo cp deploy/gold-engine.service deploy/gold-web.service /etc/systemd/system/
sudo systemctl enable --now gold-engine gold-web

sudo apt install caddy sqlite3
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile    # put your domain in it first
sudo systemctl reload caddy

journalctl -u gold-engine -f                     # watch the engine log
```

Point your domain's DNS A record at the VPS. Caddy sets up HTTPS automatically. Add `deploy/backup.sh` to cron so the database (your track record) is backed up every day.

## Go-live checklist

- [ ] Backtest results hold up across several years, after spread
- [ ] 1–2 months of paper trading with `DRY_RUN=false` into a private test channel, with the website showing those results
- [ ] Legal check in your country: selling signals can count as investment advice and may need a licence. Have `disclaimer.html` reviewed and add Terms of Service and Privacy pages
- [ ] Stripe in live mode; webhook tested end to end (subscribe → join → cancel → removed)
- [ ] Backups running; you're notified if the engine stops (for example, an UptimeRobot check on `/healthz` for the site, and log alerts for the engine)

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
  engine.py        the 24/5 loop + join-request approval
  db.py            SQLite schema and queries
  web/             FastAPI site, Stripe client, stats, templates
scripts/          backtest.py, research.py (strategy search), load_demo.py (preview data)
deploy/            systemd units, Caddyfile, backup script
tests/
```

## Known limits

- Outcomes are judged on 15-minute candles. Within a single candle the real order of moves is unknown, which is why a stop is always counted first when both are touched.
- Published results exclude spread and slippage (the website says so). Subscribers' real results will be a little worse.
- If a subscriber leaves the channel on their own, the bot can't send them a new link automatically. They need to email support, and you can clear `telegram_user_id` and `invite_link` for that row and have them reload their success page.
- The news feed covers the current week only. If it can't be fetched, signals continue by default; set `NEWS_FAIL_CLOSED=true` to pause signals instead.
- MT5 as a data source: `MT5Feed` in `data_feed.py` works on Windows with a running terminal (`pip install MetaTrader5`, and set `MT5_SERVER_UTC_OFFSET_HOURS`). It isn't wired to a setting yet; swap it in `Engine.__init__`.
