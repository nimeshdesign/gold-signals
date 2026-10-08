"""Strategy library. Every strategy turns candles into entry signals with a stop distance.

All strategies share one contract so the live engine and the backtester run identical rules:

    compute_signals(name, data, params) -> DataFrame indexed by signal-candle open time with
        signal      +1 buy / -1 sell / 0 none
        close       entry price (close of the signal candle)
        close_time  when the candle closed, i.e. when the signal is sent
        sl_dist     distance from entry to stop loss in $

`data` maps timeframe -> OHLC frame ("15min", "1h", "4h", "1day"), all indexed by candle open time (UTC).
Higher-timeframe values are joined by candle CLOSE time, so no strategy can see a candle that hasn't closed.
"""
from typing import Callable

import numpy as np
import pandas as pd

from .indicators import atr, ema, rsi
from .strategy import interval_to_timedelta

OHLC = {"open": "first", "high": "max", "low": "min", "close": "last"}
RULES = {"15min": "15min", "1h": "1h", "4h": "4h", "1day": "1D"}


# ---------- data helpers ----------

def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    return df.resample(RULES[tf], label="left", closed="left").agg(OHLC).dropna()


def build_data(base_15m: pd.DataFrame, base_1h: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """All timeframes from 15-min candles (backtest), or 15-min + 1h candles (live, longer history)."""
    hourly = base_1h if base_1h is not None else resample(base_15m, "1h")
    return {
        "15min": base_15m,
        "1h": hourly,
        "4h": resample(hourly, "4h"),
        "1day": resample(hourly, "1day"),
    }


def _close_time(df: pd.DataFrame, tf: str) -> pd.Series:
    return pd.Series(df.index + interval_to_timedelta(tf), index=df.index)


def htf_series(ltf: pd.DataFrame, ltf_tf: str, htf: pd.DataFrame, htf_tf: str, values: pd.Series) -> pd.Series:
    """Latest closed higher-timeframe value as of each lower-timeframe candle close."""
    left = pd.DataFrame({"t": _close_time(ltf, ltf_tf).values}, index=ltf.index)
    right = pd.DataFrame({"t": _close_time(htf, htf_tf).values, "v": values.values}).sort_values("t")
    merged = pd.merge_asof(left.reset_index().sort_values("t"), right, on="t", direction="backward")
    return merged.set_index(left.index.name or "index")["v"].reindex(ltf.index)


def _crossed_up(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a.shift(1) <= b.shift(1)) & (a > b)


def _crossed_down(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a.shift(1) >= b.shift(1)) & (a < b)


def _output(df: pd.DataFrame, tf: str, signal: pd.Series, sl_dist: pd.Series) -> pd.DataFrame:
    out = pd.DataFrame({
        "signal": signal.fillna(0).astype(int),
        "close": df["close"],
        "close_time": _close_time(df, tf),
        "sl_dist": sl_dist,
    }, index=df.index)
    out.loc[~(out["sl_dist"] > 0), "signal"] = 0
    return out


def _trend(data, sig_tf, trend_tf, trend_len) -> pd.Series:
    """+1 if the trend-timeframe close is above its EMA, -1 if below (0 while warming up)."""
    htf = data[trend_tf]
    e = ema(htf["close"], trend_len)
    t = np.sign(htf["close"] - e)
    t.iloc[:trend_len] = 0
    return htf_series(data[sig_tf], sig_tf, htf, trend_tf, t).fillna(0)


# ---------- strategies ----------

def ema_cross(data, p) -> pd.DataFrame:
    """Original rules: fast/slow EMA cross with the higher-timeframe trend and RSI confirmation."""
    tf = p["tf"]
    df = data[tf]
    fast, slow, r = ema(df["close"], p["fast"]), ema(df["close"], p["slow"]), rsi(df["close"], 14)
    trend = _trend(data, tf, p["trend_tf"], p["trend_len"])
    buy = (trend > 0) & _crossed_up(fast, slow) & r.between(50, 70)
    sell = (trend < 0) & _crossed_down(fast, slow) & r.between(30, 50)
    return _output(df, tf, buy.astype(int) - sell.astype(int), atr(df, 14) * p["atr_mult"])


def trend_pullback(data, p) -> pd.DataFrame:
    """Trade with the trend after a pullback: price dips below the fast EMA, then closes back above it.

    Uptrend = higher-timeframe close above its EMA and signal-timeframe close above the slow EMA.
    """
    tf = p["tf"]
    df = data[tf]
    c = df["close"]
    fast, slow = ema(c, p["fast"]), ema(c, p["slow"])
    trend = _trend(data, tf, p["trend_tf"], p["trend_len"])
    dipped = (c.shift(1) < fast.shift(1)).rolling(p["lookback"]).max().astype(bool)
    popped = (c.shift(1) > fast.shift(1)).rolling(p["lookback"]).max().astype(bool)
    buy = (trend > 0) & (c > slow) & (fast > slow) & dipped & (c > fast) & (c.shift(1) <= fast.shift(1))
    sell = (trend < 0) & (c < slow) & (fast < slow) & popped & (c < fast) & (c.shift(1) >= fast.shift(1))
    return _output(df, tf, buy.astype(int) - sell.astype(int), atr(df, 14) * p["atr_mult"])


def rsi2_reversion(data, p) -> pd.DataFrame:
    """Buy short, sharp dips in an uptrend (RSI(2) very low), sell spikes in a downtrend."""
    tf = p["tf"]
    df = data[tf]
    c = df["close"]
    r2 = rsi(c, 2)
    trend = _trend(data, tf, p["trend_tf"], p["trend_len"])
    above = c > ema(c, p["slow"])
    buy = (trend > 0) & above & (r2 < p["rsi_lo"])
    sell = (trend < 0) & ~above & (r2 > 100 - p["rsi_lo"])
    # One entry per dip: only the first candle of each oversold/overbought run.
    buy &= ~buy.shift(1, fill_value=False)
    sell &= ~sell.shift(1, fill_value=False)
    return _output(df, tf, buy.astype(int) - sell.astype(int), atr(df, 14) * p["atr_mult"])


def session_breakout(data, p) -> pd.DataFrame:
    """Asian-session range breakout during London / New York, at most one trade per day.

    Range = high/low of 15-min candles from range_start to range_end (UTC hours).
    A 15-min close beyond the range inside the trade window triggers the entry in the trend direction.
    """
    df = data["15min"]
    hour = df.index.hour
    day = df.index.normalize()
    in_range = (hour >= p["range_start"]) & (hour < p["range_end"])
    rng = df[in_range].groupby(day[in_range]).agg(hi=("high", "max"), lo=("low", "min"))
    hi = pd.Series(day, index=df.index).map(rng["hi"])
    lo = pd.Series(day, index=df.index).map(rng["lo"])
    in_window = (hour >= p["range_end"]) & (hour < p["window_end"])
    c = df["close"]
    trend = _trend(data, "15min", p["trend_tf"], p["trend_len"]) if p.get("trend_len") else pd.Series(0, index=df.index)

    up = in_window & (c > hi) & (c.shift(1) <= hi) & (trend >= 0)
    down = in_window & (c < lo) & (c.shift(1) >= lo) & (trend <= 0)
    raw = up.astype(int) - down.astype(int)
    # First breakout of the day only.
    first = raw.ne(0) & ~raw.ne(0).groupby(day).cumsum().gt(1)
    signal = raw.where(first, 0)

    width = hi - lo
    atr_h = htf_series(df, "15min", data["1h"], "1h", atr(data["1h"], 14))
    sl = (width * p["range_frac"]).clip(lower=atr_h * 0.5, upper=atr_h * p["atr_cap"])
    return _output(df, "15min", signal, sl)


def orb(data, p) -> pd.DataFrame:
    """New York opening-range breakout (Zarattini & Aziz style), at most one trade per day.

    Times are New York local, so the range follows US daylight saving. Range = high/low of the 15-min
    candles from open_hm ("HH:MM") for range_min minutes. The first 15-min close beyond the range before
    window_end (NY hour) triggers the entry, optionally only in the daily-EMA trend direction.
    Stop = range width * range_frac, kept between 0.5 and atr_cap hourly ATRs.
    """
    df = data["15min"]
    local = df.index.tz_convert("America/New_York")
    h, m = map(int, p["open_hm"].split(":"))
    minute = local.hour * 60 + local.minute
    start = h * 60 + m
    day = pd.Index(local.date)
    in_range = (minute >= start) & (minute < start + p["range_min"])
    rng = df[in_range].groupby(day[in_range]).agg(hi=("high", "max"), lo=("low", "min"))
    hi = pd.Series(day, index=df.index).map(rng["hi"])
    lo = pd.Series(day, index=df.index).map(rng["lo"])
    in_window = (minute >= start + p["range_min"]) & (local.hour < p["window_end"])
    c = df["close"]
    trend = _trend(data, "15min", p["trend_tf"], p["trend_len"]) if p.get("trend_len") else pd.Series(0, index=df.index)

    up = in_window & (c > hi) & (trend >= 0)
    down = in_window & (c < lo) & (trend <= 0)
    raw = up.astype(int) - down.astype(int)
    first = raw.ne(0) & ~raw.ne(0).groupby(day).cumsum().gt(1).to_numpy()
    signal = raw.where(first, 0)

    atr_h = htf_series(df, "15min", data["1h"], "1h", atr(data["1h"], 14))
    sl = ((hi - lo) * p["range_frac"]).clip(lower=atr_h * 0.5, upper=atr_h * p["atr_cap"])
    return _output(df, "15min", signal, sl)


STRATEGIES: dict[str, Callable] = {
    "ema_cross": ema_cross,
    "trend_pullback": trend_pullback,
    "rsi2_reversion": rsi2_reversion,
    "session_breakout": session_breakout,
    "orb": orb,
}

DEFAULT_PARAMS: dict[str, dict] = {
    "ema_cross": {"tf": "15min", "trend_tf": "4h", "trend_len": 200, "fast": 9, "slow": 21, "atr_mult": 1.5},
    "trend_pullback": {"tf": "1h", "trend_tf": "4h", "trend_len": 50, "fast": 20, "slow": 50, "lookback": 3,
                       "atr_mult": 1.5},
    "rsi2_reversion": {"tf": "1h", "trend_tf": "1day", "trend_len": 50, "slow": 50, "rsi_lo": 10, "atr_mult": 2.0},
    "session_breakout": {"range_start": 0, "range_end": 7, "window_end": 16, "trend_tf": "1day", "trend_len": 20,
                         "range_frac": 1.0, "atr_cap": 3},
    "orb": {"open_hm": "08:15", "range_min": 30, "window_end": 12, "trend_tf": "1day", "trend_len": 20,
            "range_frac": 1.0, "atr_cap": 3},
}


def compute_signals(name: str, data: dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
    return STRATEGIES[name](data, params)
