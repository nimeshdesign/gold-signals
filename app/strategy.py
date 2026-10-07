"""Rule-based XAUUSD strategy.

Rules (all evaluated on CLOSED candles only):
  * Trend filter (higher timeframe): close above the trend EMA -> buys only, below -> sells only.
  * Entry trigger (lower timeframe): fast EMA crosses the slow EMA in the trend direction,
    with RSI inside the confirmation band (momentum agrees but price is not stretched).
  * Stop loss: ATR multiple away from entry, so stops widen when gold is volatile.
  * Targets: TP1 and TP2 at fixed multiples of the risk (R).

The same `build_frame` function drives the live engine and the backtester, so the
backtest measures exactly the rules that send signals.
"""
from dataclasses import dataclass

import pandas as pd

from .config import StrategyParams
from .indicators import atr, ema, rsi

BUY = "BUY"
SELL = "SELL"


def interval_to_timedelta(interval: str) -> pd.Timedelta:
    """Convert Twelve Data style intervals ('15min', '4h', '1day') to a Timedelta."""
    s = interval.strip().lower()
    for suffix, unit in (("min", "min"), ("h", "h"), ("day", "D"), ("week", "W")):
        if s.endswith(suffix):
            return pd.Timedelta(int(s[: -len(suffix)]), unit=unit)
    raise ValueError(f"Unsupported interval: {interval}")


@dataclass
class Signal:
    direction: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    bar_time: pd.Timestamp  # open time of the LTF candle that triggered the signal
    entry_time: pd.Timestamp  # close time of that candle (when the signal is sent)
    atr: float
    rsi: float

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)


def build_frame(
    htf: pd.DataFrame,
    ltf: pd.DataFrame,
    params: StrategyParams,
    htf_interval: str,
    ltf_interval: str,
) -> pd.DataFrame:
    """Return the LTF frame with indicator columns and a `signal` column (+1 buy, -1 sell, 0 none).

    Both inputs are indexed by candle OPEN time (UTC) with open/high/low/close columns.
    The HTF trend is joined by candle CLOSE time so no future information leaks in.
    """
    htf = htf.copy()
    htf["trend_ema"] = ema(htf["close"], params.trend_ema)
    htf["trend"] = 0
    htf.loc[htf["close"] > htf["trend_ema"], "trend"] = 1
    htf.loc[htf["close"] < htf["trend_ema"], "trend"] = -1
    # Not enough history for a meaningful trend EMA yet.
    htf.iloc[: params.trend_ema, htf.columns.get_loc("trend")] = 0
    htf["close_time"] = htf.index + interval_to_timedelta(htf_interval)

    df = ltf.copy()
    df["fast"] = ema(df["close"], params.fast_ema)
    df["slow"] = ema(df["close"], params.slow_ema)
    df["rsi"] = rsi(df["close"], params.rsi_period)
    df["atr"] = atr(df, params.atr_period)
    df["close_time"] = df.index + interval_to_timedelta(ltf_interval)

    merged = pd.merge_asof(
        df.reset_index().rename(columns={df.index.name or "index": "open_time"}).sort_values("close_time"),
        htf[["close_time", "trend"]].sort_values("close_time"),
        on="close_time",
        direction="backward",
    ).set_index("open_time")
    merged["trend"] = merged["trend"].fillna(0).astype(int)

    prev_fast = merged["fast"].shift(1)
    prev_slow = merged["slow"].shift(1)
    cross_up = (prev_fast <= prev_slow) & (merged["fast"] > merged["slow"])
    cross_down = (prev_fast >= prev_slow) & (merged["fast"] < merged["slow"])

    rsi_buy = merged["rsi"].between(params.rsi_buy_min, params.rsi_buy_max)
    rsi_sell = merged["rsi"].between(params.rsi_sell_min, params.rsi_sell_max)
    warm = merged["atr"].notna() & merged["rsi"].notna()

    merged["signal"] = 0
    merged.loc[warm & (merged["trend"] == 1) & cross_up & rsi_buy, "signal"] = 1
    merged.loc[warm & (merged["trend"] == -1) & cross_down & rsi_sell, "signal"] = -1
    return merged


def signal_from_row(bar_time: pd.Timestamp, row: pd.Series, params: StrategyParams) -> Signal | None:
    if row["signal"] == 0:
        return None
    entry = float(row["close"])
    risk = float(row["atr"]) * params.atr_sl_mult
    if risk <= 0:
        return None
    d = 1 if row["signal"] == 1 else -1
    return Signal(
        direction=BUY if d == 1 else SELL,
        entry=round(entry, 2),
        sl=round(entry - d * risk, 2),
        tp1=round(entry + d * risk * params.tp1_r, 2),
        tp2=round(entry + d * risk * params.tp2_r, 2),
        bar_time=bar_time,
        entry_time=row["close_time"],
        atr=round(float(row["atr"]), 2),
        rsi=round(float(row["rsi"]), 1),
    )


def latest_signal(
    htf: pd.DataFrame,
    ltf: pd.DataFrame,
    params: StrategyParams,
    htf_interval: str,
    ltf_interval: str,
) -> Signal | None:
    """Signal on the most recent closed LTF candle, if any."""
    frame = build_frame(htf, ltf, params, htf_interval, ltf_interval)
    if frame.empty:
        return None
    return signal_from_row(frame.index[-1], frame.iloc[-1], params)
