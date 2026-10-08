"""The 24/5 signal engine.

Every 15 minutes (just after each candle closes) it:
  1. pulls fresh candles,
  2. moves open trades forward (TP1 / TP2 / SL / breakeven / expiry),
  3. checks every setup on the candles that closed since the last successful check and publishes new signals,
  4. sends the daily plan / day-end / weekly summary when they are due.
Between those checks, open trades are followed on 1-minute prices as often as the daily data allowance permits.

Design rules (see the review notes in the README):
  * No network call happens inside a database transaction (the website shares the database).
  * Telegram messages go through the `outbox` table in the same transaction as the change they report,
    then get sent; failures are retried, so nothing is lost or announced twice.
  * Weekend quotes are ignored: while the market is closed nothing is fetched or traded.

Run:  python -m app.engine            (loop forever)
      python -m app.engine --once     (one cycle, useful for testing)
"""
import argparse
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import db, market
from .config import settings
from .data_feed import DailyLimitReached, DataFeedError, TwelveDataFeed
from .indicators import ema
from .news import NewsFilter
from .outcome import TradeState, expire, step
from .strategies import DEFAULT_PARAMS, build_data, compute_signals, plan_levels
from .strategy import interval_to_timedelta
from .telegram_bot import (TelegramClient, TelegramError, format_daily_plan, format_day_end, format_signal,
                           format_update, format_week_summary)

log = logging.getLogger("engine")

M15_BARS = 1000
M15_BACKFILL = 5000   # Twelve Data maximum per request (~7 weeks of 15-min candles)
H1_BARS = 5000        # ~10 months of hourly candles for the daily trend
M15 = pd.Timedelta(minutes=15)
M1 = pd.Timedelta(minutes=1)
LATE_LIMIT = pd.Timedelta(minutes=30)   # still publish a signal this long after its candle closed
SETUP_LABELS = {"session_breakout": "Asian breakout", "orb": "NY open breakout"}


def _iso(ts) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").isoformat()


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


class Engine:
    def __init__(self, feed=None, telegram: TelegramClient | None = None, news: NewsFilter | None = None):
        self.feed = feed or TwelveDataFeed(settings.twelve_data_api_key, settings.symbol)
        self.tg = telegram or TelegramClient(settings.telegram_bot_token, dry_run=settings.dry_run)
        self.news = news or NewsFilter(settings.news_block_before, settings.news_block_after, settings.news_fail_closed)
        self.strategy_name = settings.strategy_name
        self.strategy_params = {**DEFAULT_PARAMS[self.strategy_name], **settings.strategy_params}
        self.tp1_r = settings.strategy.tp1_r
        self.tp2_r = settings.strategy.tp2_r
        self._h1: pd.DataFrame | None = None
        self._h1_at = pd.Timestamp(0, tz="UTC")
        # Extra setups (e.g. the New York open breakout) run alongside the main strategy.
        self.extra_setups = [
            {"name": e["name"], "params": {**DEFAULT_PARAMS[e["name"]], **e.get("params", {})},
             "label": e.get("label") or SETUP_LABELS.get(e["name"], e["name"]), "main": False}
            for e in settings.extra_strategies
        ]

    @property
    def setups(self) -> list[dict]:
        main = {"name": self.strategy_name, "params": self.strategy_params,
                "label": SETUP_LABELS.get(self.strategy_name, self.strategy_name), "main": True}
        return [main, *self.extra_setups]

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

    # ----- data requests and the daily allowance -----

    def _candles(self, interval: str, size: int) -> pd.DataFrame:
        """Fetch candles, counting the request against today's allowance (in its own short transaction)."""
        now = _now()
        key = f"credits_{now:%Y-%m-%d}"
        with db.session() as conn:
            used = int(db.kv_get(conn, key, "0")) + 1
            db.kv_set(conn, key, str(used))
            if used >= 0.9 * settings.daily_credit_limit:
                self.alert(conn, "credits", f"{used} of {settings.daily_credit_limit} data requests used today. "
                                            "Trade tracking is slowing down to stay within the limit.")
        try:
            return self.feed.candles(interval, size)
        except DailyLimitReached:
            with db.session() as conn:
                db.kv_set(conn, key, str(max(used, settings.daily_credit_limit)))
            raise

    def credits_today(self, conn) -> int:
        return int(db.kv_get(conn, f"credits_{_now():%Y-%m-%d}", "0"))

    def _reserved_credits(self, now: pd.Timestamp) -> int:
        """Requests still needed today for the 15-minute checks (incl. their trade check) and hourly candles."""
        midnight = now.normalize() + pd.Timedelta(days=1)
        slots = pd.date_range(now.floor("15min") + M15, midnight, freq="15min", inclusive="left")
        open_slots = sum(1 for t in slots if market.is_open(t))
        hours = math.ceil((midnight - now) / pd.Timedelta(hours=1))
        return open_slots * 2 + hours

    def track_minutes(self, conn, now: pd.Timestamp | None = None) -> int:
        """Minutes between 1-minute trade checks: as often as the rest of today's allowance can pay for.

        Never faster than TRACK_MINUTES; at least TRACK_SLOW_MINUTES once TRACK_CREDIT_BUDGET is used;
        and only from what is left after reserving the remaining 15-minute checks.
        """
        now = now or _now()
        used = self.credits_today(conn)
        minutes_left = max(1.0, (now.normalize() + pd.Timedelta(days=1) - now) / M1)
        spare = settings.daily_credit_limit - used - self._reserved_credits(now) - 10
        interval = settings.track_minutes
        if used >= settings.track_credit_budget:
            interval = max(interval, settings.track_slow_minutes)
        if spare <= 0:
            return 15  # nothing to spare: the 15-minute checks still follow open trades
        return max(interval, math.ceil(minutes_left / spare))

    # ----- one 15-minute cycle -----

    def run_cycle(self) -> None:
        now = _now()
        if not market.is_open(now):
            # Weekend: no tradeable prices. Don't spend data requests; keep the dashboard informed.
            with db.session() as conn:
                db.kv_set(conn, "engine_heartbeat", _iso(now))
                db.kv_set(conn, "market", "closed")
                if settings.daily_updates:
                    self.send_weekly(conn, now)
            self.flush_outbox()
            return

        # 1. All network requests first, outside any database transaction.
        m15 = self._candles("15min", M15_BARS)
        if self._h1 is None or now.floor("1h") > self._h1_at:
            # Hourly candles only change once an hour; reuse them in between to save data requests.
            self._h1, self._h1_at = self._candles("1h", H1_BARS), now.floor("1h")
        h1 = self._h1
        if m15.empty or h1.empty:
            log.warning("No candles returned; skipping cycle")
            return
        bars = self.fetch_tracking_bars()
        with db.session() as conn:
            backfill_needed = db.candle_count(conn) < M15_BACKFILL // 2
        history = self._candles("15min", M15_BACKFILL) if backfill_needed else None
        if settings.news_filter_enabled:
            self.news.blocking_event()  # refresh the news calendar now, not inside the transaction
        data = build_data(m15, h1)

        # 2. One short transaction for everything this cycle decides (messages queued in the outbox).
        with db.session() as conn:
            if history is not None:
                log.info("Backfilled %d candles for the chart page", db.upsert_candles(conn, history))
            db.upsert_candles(conn, m15)
            db.kv_set(conn, "market", "open")
            if bars is not None:
                self.track_live(conn, bars)
            last_ok = db.kv_get(conn, "last_cycle_ok")
            since = max(pd.Timestamp(last_ok), now - LATE_LIMIT) if last_ok else now - M15
            signals = None
            for setup in self.setups:
                try:
                    found = self.check_for_signal(conn, data, setup, since=since, now=now)
                except Exception as exc:
                    log.exception("Setup %s failed", setup["label"])
                    _record_error(f"{setup['label']} check failed: {exc!r}")
                    continue
                if setup["main"]:
                    signals = found
            snap = None
            if signals is not None:
                try:
                    snap = self.status_snapshot(data, signals, conn)
                    db.kv_set(conn, "engine_status", json.dumps(snap))
                except Exception:
                    log.exception("Could not save status snapshot")
            db.kv_set(conn, "engine_heartbeat", _iso(_now()))
            db.kv_set(conn, "last_cycle_ok", _iso(now))
            if snap and settings.daily_updates and self.strategy_name == "session_breakout":
                try:
                    self.send_daily_updates(conn, snap, now)
                except Exception as exc:
                    log.exception("Daily update failed")
                    _record_error(f"Daily Telegram update failed: {exc!r}")

        # 3. Send what was queued.
        self.flush_outbox()

    # ----- signals -----

    def check_for_signal(self, conn, data: dict[str, pd.DataFrame], setup: dict | None = None,
                         since: pd.Timestamp | None = None, now: pd.Timestamp | None = None) -> pd.DataFrame:
        """Publish signals for one setup from candles that closed after `since` (default: the last 15 minutes).

        Looking back to the last successful cycle means a failed or late cycle doesn't lose a signal.
        """
        setup = setup or self.setups[0]
        now = now or _now()
        since = since if since is not None else now - M15
        signals = compute_signals(setup["name"], data, setup["params"])
        if signals.empty:
            return signals
        fresh = signals[(signals["signal"] != 0) & (signals["close_time"] > since) & (signals["close_time"] <= now)]
        for bar_time, row in fresh.iterrows():
            self._publish(conn, setup, bar_time, row, now)
        return signals

    def _publish(self, conn, setup: dict, bar_time: pd.Timestamp, row: pd.Series, now: pd.Timestamp) -> None:
        label = setup["label"]
        entry_time = row["close_time"]
        key = _iso(bar_time) if setup["main"] else f"{_iso(bar_time)}#{setup['name']}"
        if db.signal_exists(conn, key):
            return
        if now - entry_time > LATE_LIMIT:
            log.info("%s signal on %s is too old to publish", label, bar_time)
            return
        d = int(row["signal"])
        direction = "BUY" if d == 1 else "SELL"
        entry = round(float(row["close"]), 2)
        risk = float(row["sl_dist"])
        sl = round(entry - d * risk, 2)
        tp1 = round(entry + d * risk * self.tp1_r, 2)
        tp2 = round(entry + d * risk * self.tp2_r, 2)

        if len(db.open_signals(conn)) >= settings.max_open_signals:
            self._skip(conn, entry_time, f"{label} {direction} at {entry} skipped, "
                                         f"{settings.max_open_signals} signal(s) already open")
            return
        if settings.news_filter_enabled:
            event = self.news.blocking_event()
            if event:
                self._skip(conn, entry_time, f"{label} {direction} at {entry} skipped, news blackout ({event.title})")
                return

        signal_id = db.insert_signal(
            conn, symbol=settings.display_symbol, direction=direction, entry=entry, sl=sl, tp1=tp1, tp2=tp2,
            bar_time=_iso(bar_time), created_at=_iso(entry_time),
            last_checked=_iso(entry_time - M1),  # track from the first minute after entry
            strategy=setup["name"], main=setup["main"],
        )
        if signal_id is None:
            return
        text = format_signal(settings.display_symbol, direction, entry, sl, tp1, tp2, self.tp1_r, self.tp2_r,
                             setup=label)
        late = (now - entry_time) / M1
        if late > 16:
            text = f"⏱ <b>Late signal</b>: sent {late:.0f} min after the candle closed. Check the price before entering.\n\n" + text
        db.enqueue(conn, settings.telegram_channel_id, text, kind="signal", signal_id=signal_id)
        log.info("NEW SIGNAL #%d %s %s @ %.2f SL %.2f TP1 %.2f TP2 %.2f", signal_id, label, direction,
                 entry, sl, tp1, tp2)

    def _skip(self, conn, entry_time: pd.Timestamp, text: str) -> None:
        log.info("Skipping: %s", text)
        db.kv_set(conn, "skip_note", json.dumps({"date": f"{entry_time:%Y-%m-%d}",
                                                 "text": f"{entry_time:%Y-%m-%d %H:%M} UTC: {text}"}))

    # ----- live trade tracking -----

    def fetch_tracking_bars(self) -> pd.DataFrame | None:
        """1-minute candles covering every open trade since it was last checked, or None if nothing is open."""
        with db.session() as conn:
            rows = db.open_signals(conn)
        if not rows:
            return None
        oldest = min(pd.Timestamp(r["last_checked"]) for r in rows)
        need = int((_now() - oldest) / M1) + 3
        try:
            return self._candles("1min", max(5, min(need, 5000)))
        except DailyLimitReached:
            raise
        except Exception as exc:
            # No fallback to 15-minute candles (mixing bar sizes misorders events); try again next minute.
            log.warning("1-minute prices unavailable (%s); will retry", exc)
            return None

    def track_live(self, conn, bars: pd.DataFrame | None = None) -> None:
        """Move open trades forward on 1-minute candles, so TP/SL updates go out within a minute or two."""
        if bars is None:
            bars = self.fetch_tracking_bars()
        if bars is None or bars.empty:
            return
        db.kv_set(conn, "live_price", json.dumps({"price": round(float(bars["close"].iloc[-1]), 2),
                                                  "time": _iso(bars.index[-1] + M1)}))
        self.track_open_signals(conn, bars, M1)

    def track_open_signals(self, conn, ltf: pd.DataFrame, bar_len: pd.Timedelta = M15) -> None:
        now = _now()
        expiry_minutes = settings.signal_expiry_hours * 60
        for row in db.open_signals(conn):
            state = TradeState(row["direction"], row["entry"], row["sl"], row["tp1"], row["tp2"], status=row["status"])
            new_bars = ltf[ltf.index > pd.Timestamp(row["last_checked"])]
            # A SELL closes at the ask: compare its stop/targets with chart price + spread, like a broker does.
            shift = settings.live_spread if row["direction"] == "SELL" else 0.0
            updates: dict = {}
            events: list[str] = []
            for bar_time, bar in new_bars.iterrows():
                for event in step(state, bar["high"] + shift, bar["low"] + shift, bar["open"] + shift):
                    updates["tp1_hit_at" if event == "tp1" else "closed_at"] = _iso(bar_time + bar_len)
                    events.append(event)
                updates["last_checked"] = _iso(bar_time)
                if state.is_closed:
                    break
            # The 48-hour limit counts market hours only, so a trade can't "expire" over a weekend.
            if (not state.is_closed and not ltf.empty and market.is_open(now)
                    and market.open_minutes_between(pd.Timestamp(row["created_at"]), now) >= expiry_minutes):
                expire(state, float(ltf["close"].iloc[-1]) + shift)
                updates["closed_at"] = _iso(now)
                events.append("expired")
            if updates:
                updates["status"] = state.status
                updates["result_r"] = state.result_r
                db.update_signal(conn, row["id"], **updates)
            # Queued in the same transaction as the status change: committed together or not at all.
            for event in events:
                text = format_update(event, row["direction"], row["symbol"], state.result_r)
                log.info("Signal #%d: %s", row["id"], text)
                db.enqueue(conn, settings.telegram_channel_id, text, kind="update", signal_id=row["id"])

    def track_tick(self) -> int:
        """One trade check between cycles. Returns minutes until the next check."""
        if not market.is_open(_now()):
            self.flush_outbox()
            return 5
        with db.session() as conn:
            has_open = bool(db.open_signals(conn))
            interval = self.track_minutes(conn)
        if has_open:
            bars = self.fetch_tracking_bars()
            if bars is not None:
                with db.session() as conn:
                    self.track_live(conn, bars)
        self.flush_outbox()
        return interval if has_open else 1

    # ----- Telegram outbox -----

    def flush_outbox(self) -> int:
        """Send queued Telegram messages (outside any transaction). Returns how many were sent."""
        with db.session() as conn:
            due = db.due_messages(conn)
        sent = 0
        for m in due:
            reply_to = None
            if m["kind"] == "update" and m["signal_id"]:
                with db.session() as conn:
                    waiting = conn.execute("SELECT 1 FROM outbox WHERE kind = 'signal' AND signal_id = ? "
                                           "AND status = 'pending'", (m["signal_id"],)).fetchone()
                    sig = conn.execute("SELECT telegram_msg_id FROM signals WHERE id = ?", (m["signal_id"],)).fetchone()
                if waiting:
                    continue  # never post an update before its signal
                reply_to = sig["telegram_msg_id"] if sig else None
            try:
                msg_id = self.tg.send_message(m["chat"], m["text"], reply_to=reply_to)
            except TelegramError as exc:
                backoff = exc.retry_after or min(900, 30 * 2 ** m["attempts"])
                with db.session() as conn:
                    status = db.message_failed(conn, m["id"], str(exc), backoff)
                log.warning("Telegram send failed (%s); %s", exc, "giving up" if status == "failed" else f"retry in {backoff}s")
                _record_error(f"Telegram send failed: {exc}")
                continue
            with db.session() as conn:
                db.message_sent(conn, m["id"], msg_id)
                if m["kind"] == "signal" and m["signal_id"]:
                    db.set_signal_message(conn, m["signal_id"], msg_id)
            sent += 1
        return sent

    def alert(self, conn, key: str, text: str) -> None:
        """Queue a warning for the owner, at most once per key per day."""
        marker = f"alert_{key}_{_now():%Y-%m-%d}"
        if db.kv_get(conn, marker):
            return
        db.kv_set(conn, marker, "1")
        db.enqueue(conn, settings.telegram_channel_id, f"⚠️ <b>Engine alert</b>: {text}", kind="info")
        log.warning("ALERT %s: %s", key, text)

    def alert_now(self, key: str, text: str) -> None:
        try:
            with db.session() as conn:
                self.alert(conn, key, text)
            self.flush_outbox()
        except Exception:
            log.exception("Could not send alert")

    # ----- daily messages -----

    def send_daily_updates(self, conn, snap: dict, now: pd.Timestamp | None = None) -> None:
        """Daily plan when the range is set, a day-end note, and the weekly summary. Each queued once."""
        now = now or _now()
        if now.weekday() >= 5:
            self.send_weekly(conn, now)
            return
        day = now.strftime("%Y-%m-%d")
        rng = snap["range"]
        chat = settings.telegram_channel_id

        if (rng["end"] <= now.hour < rng["window_end"] and rng["high"] is not None
                and db.kv_get(conn, "plan_sent") != day):
            events = self.news.events_between(now.normalize(), now.normalize() + pd.Timedelta(days=1)) \
                if settings.news_filter_enabled else []
            db.enqueue(conn, chat, format_daily_plan(settings.display_symbol, snap, now, events))
            db.kv_set(conn, "plan_sent", day)
            log.info("Queued daily plan")

        # Day end waits until every setup's trading window has closed.
        if now >= self._day_over(now, snap) and db.kv_get(conn, "dayend_sent") != day:
            today_rows = conn.execute("SELECT * FROM signals WHERE created_at >= ? ORDER BY id",
                                      (_iso(now.normalize()),)).fetchall()
            db.enqueue(conn, chat, format_day_end(settings.display_symbol, snap, today_rows))
            db.kv_set(conn, "dayend_sent", day)
            log.info("Queued day-end update")
            if now.weekday() == 4:
                self.send_weekly(conn, now)

    def _day_over(self, now: pd.Timestamp, snap: dict) -> pd.Timestamp:
        over = now.normalize() + pd.Timedelta(hours=snap["range"]["window_end"])
        if snap.get("ny_setup"):
            over = max(over, pd.Timestamp(snap["ny_setup"]["window_end"]))
        return over

    def send_weekly(self, conn, now: pd.Timestamp) -> None:
        """Weekly summary on Friday after the day's windows close, or caught up over the weekend."""
        if now.weekday() < 4:
            return
        monday = now.normalize() - pd.Timedelta(days=now.weekday())
        week = monday.strftime("%G-W%V")
        if db.kv_get(conn, "week_sent") == week:
            return
        if now.weekday() == 4 and db.kv_get(conn, "dayend_sent") != now.strftime("%Y-%m-%d"):
            return  # Friday: wait for the day-end message first
        week_rows = conn.execute("SELECT * FROM signals WHERE created_at >= ? AND created_at < ? ORDER BY id",
                                 (_iso(monday), _iso(monday + pd.Timedelta(days=7)))).fetchall()
        db.enqueue(conn, settings.telegram_channel_id, format_week_summary(settings.display_symbol, week_rows, monday))
        db.kv_set(conn, "week_sent", week)
        log.info("Queued weekly summary")

    # ----- candles and status -----

    def save_candles(self, conn, m15: pd.DataFrame) -> None:
        """Keep 15-min candles for the chart page (the cycle backfills ~7 weeks on first run)."""
        db.upsert_candles(conn, m15)

    def status_snapshot(self, data: dict[str, pd.DataFrame], signals: pd.DataFrame, conn=None) -> dict:
        """What the strategy sees right now, for the status page and the daily messages."""
        m15 = data["15min"]
        p = self.strategy_params
        now = _now()
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
                "window_open": p["range_end"] <= now.hour < p["window_end"] and market.is_open(now),
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
        note = json.loads(db.kv_get(conn, "skip_note") or "null") if conn is not None else None
        snap["skip_note"] = note["text"] if note and note.get("date") == f"{now:%Y-%m-%d}" else None
        snap["ny_setup"] = self.ny_session(now)
        snap["setups"] = [s["label"] for s in self.setups]
        if settings.news_filter_enabled:
            event = self.news.blocking_event()
            snap["news_block"] = f"{event.title} at {event.time:%H:%M} UTC" if event else None
        return snap


def _record_error(message: str) -> None:
    """Keep the latest engine error for the status page."""
    try:
        with db.session() as conn:
            db.kv_set(conn, "engine_last_error", json.dumps({"time": _iso(_now()), "message": message}))
    except Exception:
        log.exception("Could not record error")


def seconds_until_next_bar(interval: str, delay: float = 15) -> float:
    """Sleep until just after the next candle closes (the delay lets the data provider catch up)."""
    step_s = interval_to_timedelta(interval).total_seconds()
    now = datetime.now(timezone.utc).timestamp()
    return step_s - (now % step_s) + delay


def seconds_until_utc_midnight(delay: float = 30) -> float:
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds() + delay


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
    log.info("Engine started (dry_run=%s, symbol=%s, setups=%s, TP %sR/%sR)", engine.tg.dry_run,
             settings.symbol, [s["label"] for s in engine.setups], engine.tp1_r, engine.tp2_r)

    if args.once:
        engine.run_cycle()
        return

    next_cycle = time.time()
    next_tick = time.time() + 60
    failures = 0
    try:
        while True:
            now = time.time()
            if now >= next_cycle:
                try:
                    engine.run_cycle()
                    failures = 0
                    next_cycle = time.time() + seconds_until_next_bar("15min")
                except DailyLimitReached as exc:
                    _record_error(str(exc))
                    engine.alert_now("data_limit", "Daily data limit reached. No new signals or trade updates "
                                                   "until 00:00 UTC (5:30 AM IST). Watch open trades on your broker.")
                    next_cycle = time.time() + seconds_until_utc_midnight()
                except Exception as exc:
                    failures += 1
                    log.exception("Cycle failed (%d in a row)", failures)
                    _record_error(f"Cycle failed: {exc!r}")
                    if failures == 3:
                        engine.alert_now("cycle_failing", f"Signal checks are failing ({exc!r}). Retrying every minute.")
                    next_cycle = time.time() + 60  # retry soon; missed candles are caught up
                log.info("Next check in %.0fs", next_cycle - time.time())
                next_tick = time.time() + 60
            elif now >= next_tick:
                try:
                    minutes = engine.track_tick()
                except DailyLimitReached:
                    minutes = 15
                except Exception as exc:
                    log.exception("Live tracking failed")
                    _record_error(f"Live tracking failed: {exc!r}")
                    minutes = 1
                next_tick = time.time() + 60 * minutes
            time.sleep(max(1.0, min(next_cycle, next_tick) - time.time()))
    except KeyboardInterrupt:
        log.info("Stopped")


if __name__ == "__main__":
    main()
