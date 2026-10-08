"""Gold market hours.

Spot gold (XAUUSD) trades from Sunday 18:00 to Friday 17:00 New York time. Data vendors keep publishing
flat "quotes" over the weekend; those are not tradeable, so they are dropped everywhere (live feed,
backtests) and the 48-hour trade expiry only counts minutes when the market is open.
"""
import pandas as pd

NY = "America/New_York"
CLOSE_HOUR = 17  # Friday close, New York time
OPEN_HOUR = 18   # Sunday open, New York time


def _closed_mask(times: pd.DatetimeIndex) -> pd.Series:
    local = times.tz_convert(NY)
    wd, hour = local.weekday, local.hour
    return pd.Series((wd == 5) | ((wd == 4) & (hour >= CLOSE_HOUR)) | ((wd == 6) & (hour < OPEN_HOUR)), index=times)


def _utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def is_open(ts: pd.Timestamp) -> bool:
    return not bool(_closed_mask(pd.DatetimeIndex([_utc(ts)])).iloc[0])


def drop_closed(df: pd.DataFrame) -> pd.DataFrame:
    """Remove candles that start while the market is closed (weekend quotes)."""
    if df.empty:
        return df
    return df[~_closed_mask(df.index).to_numpy()]


def open_minutes_between(start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Minutes between two times, counting only time when the market was open."""
    start, end = _utc(start), _utc(end)
    if end <= start:
        return 0.0
    minutes = pd.date_range(start.floor("1min"), end.floor("1min"), freq="1min", inclusive="left")
    if len(minutes) == 0:
        return 0.0
    return float((~_closed_mask(minutes).to_numpy()).sum())
