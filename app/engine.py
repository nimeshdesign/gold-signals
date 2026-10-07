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
from .strategies import DEFAULT_PARAMS, build_data, compute_signals
from .strategy import interval_to_timedelta
from .telegram_bot import TelegramClient, TelegramError, format_signal, format_update

log = logging.getLogger("engine")

# 15-min candles for trade tracking and intraday rules; 1h candles (~10 months) for 4h/daily trends.
M15_BARS = 1000
H1_BARS = 5000
M15 = pd.Timedelta(minutes=15)


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

    # ----- one cycle -----

    def run_cycle(self) -> None:
        m15 = self.feed.candles("15min", M15_BARS)
        h1 = self.feed.candles("1h", H1_BARS)
        if m15.empty or h1.empty:
            log.warning("No candles returned; skipping cycle")
            return
        data = build_data(m15, h1)
        with db.session() as conn:
            self.track_open_signals(conn, m15)
            signals = self.check_for_signal(conn, data)
            try:
                db.kv_set(conn, "engine_status", json.dumps(self.status_snapshot(data, signals)))
            except Exception:
                log.exception("Could not save status snapshot")
            db.kv_set(conn, "engine_heartbeat", _iso(pd.Timestamp.now(tz="UTC")))

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
            snap["range"] = {
                "start": p["range_start"], "end": p["range_end"], "window_end": p["window_end"],
                "high": round(float(rng["high"].max()), 2) if len(rng) else None,
                "low": round(float(rng["low"].min()), 2) if len(rng) else None,
                "complete": now.hour >= p["range_end"],
                "window_open": p["range_end"] <= now.hour < p["window_end"] and now.weekday() < 5,
            }
        fired = signals[(signals["signal"] != 0) & (signals.index >= today)]
        snap["signal_today"] = None if fired.empty else {
            "time": _iso(fired["close_time"].iloc[0]),
            "direction": "BUY" if fired["signal"].iloc[0] > 0 else "SELL",
            "price": round(float(fired["close"].iloc[0]), 2),
        }
        snap["skip_note"] = self.skip_note
        if settings.news_filter_enabled:
            event = self.news.blocking_event()
            snap["news_block"] = f"{event.title} at {event.time:%H:%M} UTC" if event else None
        return snap

    def check_for_signal(self, conn, data: dict[str, pd.DataFrame]) -> pd.DataFrame:
        signals = compute_signals(self.strategy_name, data, self.strategy_params)
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
            self.skip_note = f"{entry_time:%Y-%m-%d %H:%M} UTC: {direction} at {entry} skipped, a signal is already open"
            log.info("Skipping %s signal: %d signal(s) already open", direction, settings.max_open_signals)
            return signals
        if settings.news_filter_enabled:
            event = self.news.blocking_event()
            if event:
                self.skip_note = f"{entry_time:%Y-%m-%d %H:%M} UTC: {direction} at {entry} skipped, news blackout ({event.title})"
                log.info("Skipping %s signal: news blackout for %s at %s", direction, event.title, event.time)
                return signals

        signal_id = db.insert_signal(
            conn,
            symbol=settings.display_symbol,
            direction=direction, entry=entry, sl=sl, tp1=tp1, tp2=tp2,
            bar_time=_iso(bar_time),
            created_at=_iso(entry_time),
            last_checked=_iso(entry_time - M15),
        )
        if signal_id is None:
            return signals  # already published for this candle
        conn.commit()  # persist before sending so a crash can't double-send
        log.info("NEW SIGNAL #%d %s @ %.2f SL %.2f TP1 %.2f TP2 %.2f", signal_id, direction, entry, sl, tp1, tp2)
        text = format_signal(settings.display_symbol, direction, entry, sl, tp1, tp2, self.tp1_r, self.tp2_r)
        try:
            msg_id = self.tg.send_message(settings.telegram_channel_id, text)
            db.set_signal_message(conn, signal_id, msg_id)
        except Exception as exc:
            log.exception("Failed to send signal #%d to Telegram", signal_id)
            _record_error(f"Telegram send failed for signal #{signal_id}: {exc!r}")
        return signals

    def track_open_signals(self, conn, ltf: pd.DataFrame) -> None:
        now = pd.Timestamp.now(tz="UTC")
        expiry = pd.Timedelta(hours=settings.signal_expiry_hours)
        for row in db.open_signals(conn):
            state = TradeState(row["direction"], row["entry"], row["sl"], row["tp1"], row["tp2"], status=row["status"])
            last_checked = pd.Timestamp(row["last_checked"])
            new_bars = ltf[ltf.index > last_checked]
            updates: dict = {}
            for bar_time, bar in new_bars.iterrows():
                for event in step(state, bar["high"], bar["low"]):
                    when = _iso(bar_time + M15)
                    if event == "tp1":
                        updates["tp1_hit_at"] = when
                    else:
                        updates["closed_at"] = when
                    self._announce(row, event, state.result_r)
                updates["last_checked"] = _iso(bar_time)
                if state.is_closed:
                    break
            if not state.is_closed and now - pd.Timestamp(row["created_at"]) > expiry and not ltf.empty:
                expire(state, float(ltf["close"].iloc[-1]))
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
            wait = seconds_until_next_bar("15min")
            log.info("Next check in %.0fs", wait)
            time.sleep(wait)
    except KeyboardInterrupt:
        stop.set()
        log.info("Stopped")


if __name__ == "__main__":
    main()
