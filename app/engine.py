"""The 24/5 signal engine.

Every LTF candle close it:
  1. pulls fresh candles,
  2. checks the strategy on the candle that just closed and publishes any new signal,
  3. moves open signals forward (TP1 / TP2 / SL / breakeven / expiry) and posts updates.

A second thread long-polls Telegram for join requests and approves paying subscribers.

Run:  python -m app.engine            (loop forever)
      python -m app.engine --once     (one cycle, useful for testing / cron)
"""
import argparse
import json
import logging
import threading
import time
from datetime import datetime, timezone

import pandas as pd

from . import db
from .config import settings
from .data_feed import DataFeedError, TwelveDataFeed
from .indicators import ema
from .news import NewsFilter
from .outcome import TradeState, expire, step
from .strategies import DEFAULT_PARAMS, build_data, compute_signals, plan_levels
from .strategy import interval_to_timedelta
from .telegram_bot import (TelegramClient, TelegramError, format_daily_plan, format_day_end, format_signal,
                           format_update, format_week_summary)

log = logging.getLogger("engine")

# 15-min candles for trade tracking and intraday rules; 1h candles (~10 months) for 4h/daily trends.
M15_BARS = 1000
M15_BACKFILL = 5000  # Twelve Data maximum per request (~7 weeks of 15-min candles)
SETUP_LABELS = {"session_breakout": "Asian breakout", "orb": "NY open breakout"}
H1_BARS = 5000
M15 = pd.Timedelta(minutes=15)
M1 = pd.Timedelta(minutes=1)


def _iso(ts) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").isoformat()


class Engine:
    def __init__(self, feed=None, telegram: TelegramClient | None = None, news: NewsFilter | None = None):
        self.feed = feed or TwelveDataFeed(settings.twelve_data_api_key, settings.symbol)
        self.tg = telegram or TelegramClient(settings.telegram_bot_token, dry_run=settings.dry_run)
        self.news = news or NewsFilter(settings.news_block_before, settings.news_block_after, settings.news_fail_closed)
        self.strategy_name = settings.strategy_name
        self.strategy_params = {**DEFAULT_PARAMS[self.strategy_name], **settings.strategy_params}
        self.tp1_r = settings.strategy.tp1_r
        self.tp2_r = settings.strategy.tp2_r
        self.skip_note: str | None = None
        self._h1: pd.DataFrame | None = None
        self._h1_at = pd.Timestamp(0, tz="UTC")
        # Extra setups (e.g. the New York open breakout) run alongside the main strategy.
        self.extra_setups = [
            {"name": e["name"], "params": {**DEFAULT_PARAMS[e["name"]], **e.get("params", {})},
             "label": e.get("label") or SETUP_LABELS.get(e["name"], e["name"]), "main": False}
            for e in settings.extra_strategies
        ]

    def ny_session(self, now: pd.Timestamp) -> dict | None:
        """Today's New York open-breakout times in UTC (they move with US daylight saving)."""
        orb_setup = next((s for s in self.extra_setups if s["name"] == "orb"), None)
        if orb_setup is None:
            return None
        p = orb_setup["params"]
        day = now.tz_convert("America/New_York").strftime("%Y-%m-%d")
        start = pd.Timestamp(f"{day} {p['open_hm']}", tz="America/New_York").tz_convert("UTC")
        return {"range_start": _iso(start), "range_end": _iso(start + pd.Timedelta(minutes=p["range_min"])),
                "window_end": _iso(pd.Timestamp(f"{day} {p['window_end']:02d}:00",
                                                tz="America/New_York").tz_convert("UTC"))}

    @property
    def setups(self) -> list[dict]:
        main = {"name": self.strategy_name, "params": self.strategy_params,
                "label": SETUP_LABELS.get(self.strategy_name, self.strategy_name), "main": True}
        return [main, *self.extra_setups]

    # ----- one cycle -----

    def run_cycle(self) -> None:
        with db.session() as conn:
            m15 = self._candles(conn, "15min", M15_BARS)
            # Hourly candles only change once an hour; reuse them in between to save data requests.
            now = pd.Timestamp.now(tz="UTC")
            if self._h1 is None or now.floor("1h") > self._h1_at:
                self._h1, self._h1_at = self._candles(conn, "1h", H1_BARS), now.floor("1h")
            h1 = self._h1
        if m15.empty or h1.empty:
            log.warning("No candles returned; skipping cycle")
            return
        data = build_data(m15, h1)
        with db.session() as conn:
            self.save_candles(conn, m15)
            try:
                self.track_live(conn, fallback=m15)
            except Exception as exc:
                log.exception("Trade tracking failed")
                _record_error(f"Trade tracking failed: {exc!r}")
            signals = self.check_for_signal(conn, data)
            for setup in self.setups[1:]:
                try:
                    self.check_for_signal(conn, data, setup)
                except Exception as exc:
                    log.exception("Setup %s failed", setup["label"])
                    _record_error(f"{setup['label']} check failed: {exc!r}")
            snap = None
            try:
                snap = self.status_snapshot(data, signals)
                db.kv_set(conn, "engine_status", json.dumps(snap))
            except Exception:
                log.exception("Could not save status snapshot")
            db.kv_set(conn, "engine_heartbeat", _iso(pd.Timestamp.now(tz="UTC")))
            if snap and settings.daily_updates and self.strategy_name == "session_breakout":
                try:
                    self.send_daily_updates(conn, snap)
                except Exception as exc:
                    log.exception("Daily update failed")
                    _record_error(f"Daily Telegram update failed: {exc!r}")

    def send_daily_updates(self, conn, snap: dict, now: pd.Timestamp | None = None) -> None:
        """Daily plan when the range is set, a day-end note, and a Friday weekly summary. Each sent once."""
        now = now or pd.Timestamp.now(tz="UTC")
        if now.weekday() >= 5:
            return
        day = now.strftime("%Y-%m-%d")
        rng = snap["range"]
        chat = settings.telegram_channel_id

        if (rng["end"] <= now.hour < rng["window_end"] and rng["high"] is not None
                and db.kv_get(conn, "plan_sent") != day):
            events = self.news.events_between(now.normalize(), now.normalize() + pd.Timedelta(days=1)) \
                if settings.news_filter_enabled else []
            self.tg.send_message(chat, format_daily_plan(settings.display_symbol, snap, now, events))
            db.kv_set(conn, "plan_sent", day)
            log.info("Sent daily plan")

        # Day end waits until every setup's trading window has closed.
        day_over = now.normalize() + pd.Timedelta(hours=rng["window_end"])
        if snap.get("ny_setup"):
            day_over = max(day_over, pd.Timestamp(snap["ny_setup"]["window_end"]))
        if now >= day_over and db.kv_get(conn, "plan_sent") == day and db.kv_get(conn, "dayend_sent") != day:
            today_rows = conn.execute("SELECT * FROM signals WHERE created_at >= ? ORDER BY id",
                                      (_iso(now.normalize()),)).fetchall()
            self.tg.send_message(chat, format_day_end(settings.display_symbol, snap, today_rows))
            db.kv_set(conn, "dayend_sent", day)
            log.info("Sent day-end update")

            week = now.strftime("%G-W%V")
            if now.weekday() == 4 and db.kv_get(conn, "week_sent") != week:
                monday = now.normalize() - pd.Timedelta(days=now.weekday())
                week_rows = conn.execute("SELECT * FROM signals WHERE created_at >= ? ORDER BY id",
                                         (_iso(monday),)).fetchall()
                self.tg.send_message(chat, format_week_summary(settings.display_symbol, week_rows, monday))
                db.kv_set(conn, "week_sent", week)
                log.info("Sent weekly summary")

    def save_candles(self, conn, m15: pd.DataFrame) -> None:
        """Keep 15-min candles for the chart page. The first run backfills ~7 weeks (enough for the daily trend)."""
        try:
            if db.candle_count(conn) < M15_BACKFILL // 2:
                history = self._candles(conn, "15min", M15_BACKFILL)
                log.info("Backfilled %d candles for the chart page", db.upsert_candles(conn, history))
            db.upsert_candles(conn, m15)
        except Exception:
            log.exception("Could not save candles")

    def status_snapshot(self, data: dict[str, pd.DataFrame], signals: pd.DataFrame) -> dict:
        """What the strategy sees right now, for the owner's status page."""
        m15 = data["15min"]
        p = self.strategy_params
        now = pd.Timestamp.now(tz="UTC")
        today = now.normalize()
        snap: dict = {
            "updated": _iso(now),
            "price": round(float(m15["close"].iloc[-1]), 2),
            "price_time": _iso(m15.index[-1] + M15),
            "strategy": self.strategy_name,
            "params": p,
            "tp1_r": self.tp1_r,
            "tp2_r": self.tp2_r,
        }
        if p.get("trend_tf") and p.get("trend_len"):
            htf = data[p["trend_tf"]]
            closed = htf[htf.index + interval_to_timedelta(p["trend_tf"]) <= now]
            trend_ema = ema(closed["close"], p["trend_len"])
            snap["trend"] = {
                "label": f"{p['trend_tf']} close vs {p['trend_len']} EMA",
                "close": round(float(closed["close"].iloc[-1]), 2),
                "ema": round(float(trend_ema.iloc[-1]), 2),
                "direction": "UP" if closed["close"].iloc[-1] > trend_ema.iloc[-1] else "DOWN",
            }
        if self.strategy_name == "session_breakout":
            day = m15[m15.index >= today]
            rng = day[(day.index.hour >= p["range_start"]) & (day.index.hour < p["range_end"])]
            after = day[(day.index.hour >= p["range_end"]) & (day.index.hour < p["window_end"])]
            snap["window_moves"] = None if after.empty else {
                "high": round(float(after["high"].max()), 2), "low": round(float(after["low"].min()), 2),
                "close_high": round(float(after["close"].max()), 2), "close_low": round(float(after["close"].min()), 2),
            }
            snap["range"] = {
                "start": p["range_start"], "end": p["range_end"], "window_end": p["window_end"],
                "high": round(float(rng["high"].max()), 2) if len(rng) else None,
                "low": round(float(rng["low"].min()), 2) if len(rng) else None,
                "complete": now.hour >= p["range_end"],
                "window_open": p["range_end"] <= now.hour < p["window_end"] and now.weekday() < 5,
            }
        if snap.get("range") and snap.get("trend") and snap["range"]["high"] is not None:
            up = snap["trend"]["direction"] == "UP"
            trigger = {"side": "BUY" if up else "SELL", "price": snap["range"]["high"] if up else snap["range"]["low"]}
            snap["planned"] = plan_levels(signals, today, trigger, p, self.tp1_r, self.tp2_r)
        fired = signals[(signals["signal"] != 0) & (signals.index >= today)]
        snap["signal_today"] = None if fired.empty else {
            "time": _iso(fired["close_time"].iloc[0]),
            "direction": "BUY" if fired["signal"].iloc[0] > 0 else "SELL",
            "price": round(float(fired["close"].iloc[0]), 2),
        }
        snap["skip_note"] = self.skip_note
        snap["ny_setup"] = self.ny_session(now)
        snap["setups"] = [s["label"] for s in self.setups]
        if settings.news_filter_enabled:
            event = self.news.blocking_event()
            snap["news_block"] = f"{event.title} at {event.time:%H:%M} UTC" if event else None
        return snap

    def check_for_signal(self, conn, data: dict[str, pd.DataFrame], setup: dict | None = None) -> pd.DataFrame:
        """Publish a new signal for one setup (the main strategy unless `setup` is given)."""
        setup = setup or self.setups[0]
        signals = compute_signals(setup["name"], data, setup["params"])
        label = setup["label"]
        if signals.empty:
            return signals
        last = signals.iloc[-1]
        if last["signal"] == 0:
            return signals
        bar_time = signals.index[-1]
        entry_time = last["close_time"]
        # Only act on a candle that closed in the last 15 minutes (e.g. not after a restart or weekend gap).
        age = pd.Timestamp.now(tz="UTC") - entry_time
        if age > M15:
            log.info("Signal on %s is %s old; not publishing", bar_time, age)
            return signals
        d = int(last["signal"])
        direction = "BUY" if d == 1 else "SELL"
        entry = round(float(last["close"]), 2)
        risk = float(last["sl_dist"])
        sl = round(entry - d * risk, 2)
        tp1 = round(entry + d * risk * self.tp1_r, 2)
        tp2 = round(entry + d * risk * self.tp2_r, 2)

        if len(db.open_signals(conn)) >= settings.max_open_signals:
            self.skip_note = (f"{entry_time:%Y-%m-%d %H:%M} UTC: {label} {direction} at {entry} skipped, "
                              f"{settings.max_open_signals} signal(s) already open")
            log.info("Skipping %s %s signal: %d signal(s) already open", label, direction, settings.max_open_signals)
            return signals
        if settings.news_filter_enabled:
            event = self.news.blocking_event()
            if event:
                self.skip_note = (f"{entry_time:%Y-%m-%d %H:%M} UTC: {label} {direction} at {entry} skipped, "
                                  f"news blackout ({event.title})")
                log.info("Skipping %s %s signal: news blackout for %s at %s", label, direction, event.title, event.time)
                return signals

        signal_id = db.insert_signal(
            conn,
            symbol=settings.display_symbol,
            direction=direction, entry=entry, sl=sl, tp1=tp1, tp2=tp2,
            bar_time=_iso(bar_time),
            created_at=_iso(entry_time),
            last_checked=_iso(entry_time - M1),  # track from the first minute after entry
            strategy=setup["name"], main=setup["main"],
        )
        if signal_id is None:
            return signals  # already published for this candle
        conn.commit()  # persist before sending so a crash can't double-send
        log.info("NEW SIGNAL #%d %s %s @ %.2f SL %.2f TP1 %.2f TP2 %.2f", signal_id, label, direction,
                 entry, sl, tp1, tp2)
        text = format_signal(settings.display_symbol, direction, entry, sl, tp1, tp2, self.tp1_r, self.tp2_r,
                             setup=label)
        try:
            msg_id = self.tg.send_message(settings.telegram_channel_id, text)
            db.set_signal_message(conn, signal_id, msg_id)
        except Exception as exc:
            log.exception("Failed to send signal #%d to Telegram", signal_id)
            _record_error(f"Telegram send failed for signal #{signal_id}: {exc!r}")
        return signals

    # ----- live trade tracking -----

    def _candles(self, conn, interval: str, size: int) -> pd.DataFrame:
        """Fetch candles and count the Twelve Data request against today's budget."""
        key = f"credits_{pd.Timestamp.now(tz='UTC'):%Y-%m-%d}"
        db.kv_set(conn, key, str(int(db.kv_get(conn, key, "0")) + 1))
        return self.feed.candles(interval, size)

    def credits_today(self, conn) -> int:
        return int(db.kv_get(conn, f"credits_{pd.Timestamp.now(tz='UTC'):%Y-%m-%d}", "0"))

    def track_minutes(self, conn) -> int:
        """How often to check open trades: every minute, slower once near the daily data limit."""
        if self.credits_today(conn) >= settings.track_credit_budget:
            return settings.track_slow_minutes
        return settings.track_minutes

    def track_live(self, conn, fallback: pd.DataFrame | None = None) -> None:
        """Move open trades forward on 1-minute candles, so TP/SL updates go out within a minute or two."""
        rows = db.open_signals(conn)
        if not rows:
            return
        oldest = min(pd.Timestamp(r["last_checked"]) for r in rows)
        need = int((pd.Timestamp.now(tz="UTC") - oldest) / M1) + 3
        try:
            m1 = self._candles(conn, "1min", max(5, min(need, 5000)))
            bars, bar_len = m1, M1
        except Exception as exc:
            if fallback is None:
                raise
            log.warning("1-minute prices unavailable (%s); tracking on 15-minute candles", exc)
            bars, bar_len = fallback, M15
        if bars.empty:
            return
        last = bars.iloc[-1]
        db.kv_set(conn, "live_price", json.dumps({"price": round(float(last["close"]), 2),
                                                  "time": _iso(bars.index[-1] + bar_len)}))
        self.track_open_signals(conn, bars, bar_len)

    def track_open_signals(self, conn, ltf: pd.DataFrame, bar_len: pd.Timedelta = M15) -> None:
        now = pd.Timestamp.now(tz="UTC")
        expiry = pd.Timedelta(hours=settings.signal_expiry_hours)
        for row in db.open_signals(conn):
            state = TradeState(row["direction"], row["entry"], row["sl"], row["tp1"], row["tp2"], status=row["status"])
            last_checked = pd.Timestamp(row["last_checked"])
            new_bars = ltf[ltf.index > last_checked]
            # A SELL closes at the ask: compare its stop/targets with chart price + spread, like a broker does.
            shift = settings.live_spread if row["direction"] == "SELL" else 0.0
            updates: dict = {}
            for bar_time, bar in new_bars.iterrows():
                for event in step(state, bar["high"] + shift, bar["low"] + shift):
                    when = _iso(bar_time + bar_len)
                    if event == "tp1":
                        updates["tp1_hit_at"] = when
                    else:
                        updates["closed_at"] = when
                    self._announce(row, event, state.result_r)
                updates["last_checked"] = _iso(bar_time)
                if state.is_closed:
                    break
            if not state.is_closed and now - pd.Timestamp(row["created_at"]) > expiry and not ltf.empty:
                expire(state, float(ltf["close"].iloc[-1]) + shift)
                updates["closed_at"] = _iso(now)
                self._announce(row, "expired", state.result_r)
            if updates:
                updates["status"] = state.status
                updates["result_r"] = state.result_r
                db.update_signal(conn, row["id"], **updates)

    def _announce(self, row, event: str, result_r: float | None) -> None:
        text = format_update(event, row["direction"], row["symbol"], result_r)
        log.info("Signal #%d: %s", row["id"], text)
        try:
            self.tg.send_message(settings.telegram_channel_id, text, reply_to=row["telegram_msg_id"])
            if event != "tp1" and settings.telegram_public_channel_id:
                self.tg.send_message(settings.telegram_public_channel_id, text)
        except Exception as exc:
            log.exception("Failed to send update for signal #%d", row["id"])
            _record_error(f"Telegram update failed for signal #{row['id']}: {exc!r}")

    # ----- Telegram join requests -----

    def handle_join_requests_forever(self, stop: threading.Event) -> None:
        if self.tg.dry_run:
            log.info("Telegram dry run: join-request handler disabled")
            return
        while not stop.is_set():
            try:
                self.handle_join_requests_once()
            except Exception:
                log.exception("Join request polling failed")
                stop.wait(10)

    def handle_join_requests_once(self, poll_timeout: int = 50) -> None:
        with db.session() as conn:
            offset = db.kv_get(conn, "telegram_offset")
        updates = self.tg.get_updates(int(offset) if offset else None, timeout=poll_timeout)
        for upd in updates:
            req = upd.get("chat_join_request")
            if req:
                self._process_join_request(req)
            with db.session() as conn:
                db.kv_set(conn, "telegram_offset", str(upd["update_id"] + 1))

    def _process_join_request(self, req: dict) -> None:
        chat_id = req["chat"]["id"]
        user_id = req["from"]["id"]
        link = (req.get("invite_link") or {}).get("invite_link")
        with db.session() as conn:
            sub = db.get_subscriber_by(conn, "invite_link", link) if link else None
            if sub and sub["status"] == "active":
                self.tg.approve_join_request(chat_id, user_id)
                db.update_subscriber(conn, sub["id"], telegram_user_id=user_id)
                # One link, one person: revoke so a shared link can't let others in.
                try:
                    self.tg.revoke_invite_link(chat_id, link)
                except TelegramError:
                    log.exception("Could not revoke invite link for subscriber #%d", sub["id"])
                log.info("Approved Telegram user %s for subscriber #%d", user_id, sub["id"])
            else:
                self.tg.decline_join_request(chat_id, user_id)
                log.info("Declined Telegram user %s (link %s not tied to an active subscription)", user_id, link)


def _record_error(message: str) -> None:
    """Keep the latest engine error for the status page."""
    try:
        with db.session() as conn:
            db.kv_set(conn, "engine_last_error", json.dumps({"time": _iso(pd.Timestamp.now(tz="UTC")), "message": message}))
    except Exception:
        log.exception("Could not record error")


def seconds_until_next_bar(interval: str, delay: float = 15) -> float:
    """Sleep until just after the next candle closes (the delay lets the data provider catch up)."""
    step_s = interval_to_timedelta(interval).total_seconds()
    now = datetime.now(timezone.utc).timestamp()
    return step_s - (now % step_s) + delay


def main() -> None:
    parser = argparse.ArgumentParser(description="XAUUSD signal engine")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_db()
    try:
        engine = Engine()
    except DataFeedError as exc:
        raise SystemExit(f"Config error: {exc}. Copy .env.example to .env and fill it in.") from None
    log.info("Engine started (dry_run=%s, symbol=%s, strategy=%s %s, TP %sR/%sR)", engine.tg.dry_run,
             settings.symbol, engine.strategy_name, engine.strategy_params, engine.tp1_r, engine.tp2_r)

    if args.once:
        engine.run_cycle()
        return

    stop = threading.Event()
    threading.Thread(target=engine.handle_join_requests_forever, args=(stop,), daemon=True).start()
    try:
        while True:
            try:
                engine.run_cycle()
            except Exception as exc:
                log.exception("Cycle failed")
                _record_error(f"Cycle failed: {exc!r}")
            next_cycle = time.time() + seconds_until_next_bar("15min")
            log.info("Next check in %.0fs", next_cycle - time.time())
            # Between 15-minute signal checks, follow open trades on 1-minute prices.
            while time.time() < next_cycle:
                pause = 60.0
                try:
                    with db.session() as conn:
                        if db.open_signals(conn):
                            engine.track_live(conn)
                            pause = 60.0 * engine.track_minutes(conn)
                except Exception as exc:
                    log.exception("Live tracking failed")
                    _record_error(f"Live tracking failed: {exc!r}")
                time.sleep(max(1.0, min(pause, next_cycle - time.time())))
    except KeyboardInterrupt:
        stop.set()
        log.info("Stopped")


if __name__ == "__main__":
    main()
