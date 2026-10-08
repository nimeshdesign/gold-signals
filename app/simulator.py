"""Trade simulator shared by the backtest and strategy research.

Signals can come from any timeframe; trades are always managed on 15-minute candles using the same
`outcome.step` rules as the live tracker (half at TP1, stop to entry, rest to TP2).

Spread model (chart prices are bid prices, as with most gold feeds):
  * BUY fills at ask = close + spread and exits at bid, so the levels are checked on the bid candle
    and the result is reduced by spread / risk.
  * SELL fills at bid = close and exits at ask, so the levels are checked on the candle shifted up by
    the spread (a sell's stop is hit sooner and its targets later).
"""
from dataclasses import dataclass

import pandas as pd

from .outcome import TradeState, expire, step

BAR = pd.Timedelta(minutes=15)


@dataclass
class SimConfig:
    tp1_r: float = 1.0
    tp2_r: float = 2.0
    spread: float = 0.30
    max_open: int = 1
    expiry_hours: float = 48
    one_per_direction: bool = False         # skip a signal if a trade in the same direction is already open
    friday_cutoff_hour: int | None = None   # no new trades on Friday from this UTC hour


def simulate(signals: pd.DataFrame, m15: pd.DataFrame, cfg: SimConfig, bar: pd.Timedelta = BAR) -> pd.DataFrame:
    """Run every signal through the trade rules. Returns one row per closed trade.

    `m15` is the candle series used to manage trades. Pass 1-minute candles with bar=1 minute to resolve
    tight stops/targets that a single 15-minute candle can't order (signals still come from 15-min closes).
    """
    sig = signals[signals["signal"] != 0]
    # Several setups can signal on the same candle; the live engine takes them in order (main first).
    by_time: dict = {}
    for row in sig.itertuples():
        by_time.setdefault(row.close_time, []).append(row)
    opens, highs, lows, closes = (m15[k].to_numpy() for k in ("open", "high", "low", "close"))
    times = m15.index
    # Expiry counts candles, i.e. market time only (weekend quotes are not in the data).
    expiry_bars = int(pd.Timedelta(hours=cfg.expiry_hours) / bar)

    open_trades: list[tuple] = []
    out = []
    for i in range(len(m15)):
        bar_close = times[i] + bar
        still = []
        for row, st, start_i in open_trades:
            shift = cfg.spread if st.direction == "SELL" else 0.0
            step(st, highs[i] + shift, lows[i] + shift, opens[i] + shift)
            if not st.is_closed and i - start_i + 1 >= expiry_bars:
                expire(st, closes[i] + shift)
            if st.is_closed:
                out.append(_record(row, st, bar_close, cfg))
            else:
                still.append((row, st, start_i))
        open_trades = still

        for row in by_time.get(bar_close, []):
            if len(open_trades) >= cfg.max_open:
                break
            if cfg.friday_cutoff_hour is not None and bar_close.weekday() == 4 and bar_close.hour >= cfg.friday_cutoff_hour:
                continue
            d = row.signal
            if cfg.one_per_direction and any(st.sign == d for _, st, _ in open_trades):
                continue
            entry, risk = round(row.close, 2), row.sl_dist
            st = TradeState(
                "BUY" if d == 1 else "SELL", entry,
                sl=entry - d * risk, tp1=entry + d * risk * cfg.tp1_r, tp2=entry + d * risk * cfg.tp2_r,
            )
            open_trades.append((row, st, i + 1))
    return pd.DataFrame(out)


def _record(row, st: TradeState, closed_at, cfg: SimConfig) -> dict:
    cost = cfg.spread / st.risk if st.direction == "BUY" else 0.0
    return {
        "entry_time": row.close_time, "closed_at": closed_at, "direction": st.direction,
        "entry": st.entry, "sl": round(st.sl, 2), "tp1": round(st.tp1, 2), "tp2": round(st.tp2, 2),
        "risk": round(st.risk, 2), "outcome": st.status, "strategy": getattr(row, "strategy", None),
        # Live-tracker R: SELL spread is already inside (stops/targets checked at the ask); BUY spread is not.
        "result_r_gross": st.result_r,
        "result_r": round(st.result_r - cost, 3),
    }


def metrics(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0, "win_rate": 0.0, "net_r": 0.0, "avg_r": 0.0, "pf": 0.0, "max_dd": 0.0}
    r = trades["result_r"]
    eq = r.cumsum()
    gross_loss = -r[r < 0].sum()
    return {
        "trades": len(r),
        "win_rate": round((r > 0).mean() * 100, 1),
        "net_r": round(r.sum(), 1),
        "avg_r": round(r.mean(), 3),
        "pf": round(r[r > 0].sum() / gross_loss, 2) if gross_loss else float("inf"),
        "max_dd": round((eq.cummax().clip(lower=0) - eq).max(), 1),
    }
