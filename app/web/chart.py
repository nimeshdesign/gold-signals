"""Data for the chart page: one day's candles, the strategy's levels, and any signal with its outcome.

Everything is recomputed from stored candles with the live strategy code, so the chart shows exactly
what the engine sees: the Asian range, the trend, the trigger level, and the trade's entry/SL/TP levels.
"""
from datetime import date

import pandas as pd

from .. import db
from ..indicators import ema
from ..outcome import TradeState, step
from ..strategies import build_data, compute_signals

M15 = pd.Timedelta(minutes=15)
HISTORY_DAYS = 45  # enough daily candles for the 20-day trend EMA
OUTCOMES = {"open": "Open", "tp1": "TP1 hit, running", "sl": "Stop loss hit", "be": "TP1, then stopped at entry",
            "tp2": "TP1 and TP2 hit", "expired": "Closed at the 48-hour limit"}


def _unix(ts: pd.Timestamp, offset_hours: float) -> int:
    """Chart time: the chart library has no time zones, so shift UTC by the display offset."""
    return int((ts + pd.Timedelta(hours=offset_hours)).timestamp())


def replay(row, candles: pd.DataFrame) -> list[dict]:
    """Walk a signal through the candles after its entry to find when TP1 / TP2 / SL happened."""
    st = TradeState(row["direction"], row["entry"], row["sl"], row["tp1"], row["tp2"])
    entry_time = pd.Timestamp(row["created_at"])
    events = []
    for ts, c in candles[candles.index >= entry_time].iterrows():
        for ev in step(st, c["high"], c["low"]):
            events.append({"time": ts, "event": ev})
        if st.is_closed:
            break
    return events


def day_view(conn, day: date, params: dict, tp1_r: float, tp2_r: float, offset_hours: float) -> dict | None:
    start = pd.Timestamp(day, tz="UTC")
    m15 = db.load_candles(conn, (start - pd.Timedelta(days=HISTORY_DAYS)).isoformat(),
                          (start + pd.Timedelta(days=3)).isoformat())
    today = m15[(m15.index >= start) & (m15.index < start + pd.Timedelta(days=1))]
    if today.empty:
        return None

    # Trend: the last daily close before this day vs its EMA (what the strategy uses all day).
    data = build_data(m15[m15.index < start + pd.Timedelta(days=1)])
    daily = data["1day"]
    prev_days = daily[daily.index < start]
    trend = None
    if params.get("trend_len") and len(prev_days) >= params["trend_len"]:
        e = ema(prev_days["close"], params["trend_len"])
        trend = {"direction": "UP" if prev_days["close"].iloc[-1] > e.iloc[-1] else "DOWN",
                 "close": round(float(prev_days["close"].iloc[-1]), 2), "ema": round(float(e.iloc[-1]), 2),
                 "length": params["trend_len"]}

    rng = today[(today.index.hour >= params["range_start"]) & (today.index.hour < params["range_end"])]
    range_hi = round(float(rng["high"].max()), 2) if len(rng) else None
    range_lo = round(float(rng["low"].min()), 2) if len(rng) else None
    trigger = None
    if trend and range_hi is not None:
        trigger = {"side": "BUY" if trend["direction"] == "UP" else "SELL",
                   "price": range_hi if trend["direction"] == "UP" else range_lo}

    # What the strategy computed for this day (fires even if the engine wasn't running then).
    signals = compute_signals("session_breakout", data, params)
    fired = signals[(signals["signal"] != 0) & (signals.index >= start)]
    strategy_signal = None
    if not fired.empty:
        f = fired.iloc[0]
        strategy_signal = {"time": f["close_time"], "side": "BUY" if f["signal"] > 0 else "SELL",
                           "price": round(float(f["close"]), 2)}

    rows = conn.execute("SELECT * FROM signals WHERE created_at >= ? AND created_at < ? ORDER BY id",
                        (start.isoformat(), (start + pd.Timedelta(days=1)).isoformat())).fetchall()
    trades = []
    end = start + pd.Timedelta(days=1)
    for r in rows:
        events = replay(r, m15)
        closed = pd.Timestamp(r["closed_at"]) if r["closed_at"] else None
        if events:
            end = max(end, events[-1]["time"] + 4 * M15)
        elif closed is not None:
            end = max(end, closed + 4 * M15)
        trades.append({"row": dict(r), "events": events, "outcome": OUTCOMES.get(r["status"], r["status"]),
                       "risk": round(abs(r["entry"] - r["sl"]), 2)})
    end = min(end, start + pd.Timedelta(days=3))
    shown = m15[(m15.index >= start) & (m15.index < end)]

    # ---- chart payload ----
    levels, markers = [], []
    if range_hi is not None:
        levels += [{"price": range_hi, "title": "Range high", "kind": "range"},
                   {"price": range_lo, "title": "Range low", "kind": "range"}]
    if trigger and not trades:
        levels.append({"price": trigger["price"], "title": f"{trigger['side']} trigger", "kind": "trigger"})
    for t in trades:
        r = t["row"]
        levels += [{"price": r["entry"], "title": f"Entry {r['direction']}", "kind": "entry"},
                   {"price": r["sl"], "title": "Stop loss", "kind": "sl"},
                   {"price": r["tp1"], "title": "TP1", "kind": "tp"},
                   {"price": r["tp2"], "title": "TP2", "kind": "tp"}]
        buy = r["direction"] == "BUY"
        markers.append({"time": _unix(pd.Timestamp(r["created_at"]) - M15, offset_hours),
                        "position": "belowBar" if buy else "aboveBar", "shape": "arrowUp" if buy else "arrowDown",
                        "kind": "entry", "text": f"{r['direction']} {r['entry']:.2f}"})
        for ev in t["events"]:
            label = {"tp1": "TP1", "tp2": "TP2", "sl": "SL", "be": "Stop at entry"}.get(ev["event"], ev["event"])
            markers.append({"time": _unix(ev["time"], offset_hours), "position": "aboveBar" if buy else "belowBar",
                            "shape": "circle", "kind": "sl" if ev["event"] == "sl" else "tp", "text": label})
    if strategy_signal and not trades:
        buy = strategy_signal["side"] == "BUY"
        markers.append({"time": _unix(strategy_signal["time"] - M15, offset_hours),
                        "position": "belowBar" if buy else "aboveBar", "shape": "arrowUp" if buy else "arrowDown",
                        "kind": "strategy", "text": f"{strategy_signal['side']} (not sent)"})
    markers.sort(key=lambda m: m["time"])

    return {
        "day": day,
        "candles": [[_unix(ts, offset_hours), round(c.open, 2), round(c.high, 2), round(c.low, 2), round(c.close, 2)]
                    for ts, c in zip(shown.index, shown.itertuples())],
        "levels": levels,
        "markers": markers,
        "trend": trend,
        "range": {"high": range_hi, "low": range_lo,
                  "width": round(range_hi - range_lo, 2) if range_hi is not None else None},
        "trigger": trigger,
        "strategy_signal": strategy_signal,
        "trades": trades,
        "day_high": round(float(today["high"].max()), 2),
        "day_low": round(float(today["low"].min()), 2),
        "last_close": round(float(today["close"].iloc[-1]), 2),
        "tp1_r": tp1_r, "tp2_r": tp2_r,
    }


def neighbours(days: list[str], day: date) -> tuple[str | None, str | None]:
    """Previous and next dates that have candles (weekends skipped automatically)."""
    s = day.isoformat()
    prev = [d for d in days if d < s]
    nxt = [d for d in days if d > s]
    return (prev[-1] if prev else None), (nxt[0] if nxt else None)


def parse_day(value: str | None, days: list[str]) -> date | None:
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return date.fromisoformat(days[-1]) if days else None


def signal_day(conn, signal_id: int) -> date | None:
    row = conn.execute("SELECT created_at FROM signals WHERE id = ?", (signal_id,)).fetchone()
    if not row:
        return None
    ts = pd.Timestamp(row["created_at"]).tz_convert("UTC")
    return (ts - M15).date() if ts.hour == 0 and ts.minute == 0 else ts.date()


