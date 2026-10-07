"""Price data. Twelve Data is the default; MetaTrader 5 works too if you run the engine on Windows next to MT5."""
import logging
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from .strategy import interval_to_timedelta

log = logging.getLogger(__name__)

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"
MAX_OUTPUTSIZE = 5000


class DataFeedError(RuntimeError):
    pass


def _to_frame(values: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(values)
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.set_index("datetime").sort_index()
    df.index.name = "datetime"
    df = df[["open", "high", "low", "close"]].astype(float)
    return df[~df.index.duplicated(keep="last")]


def drop_incomplete(df: pd.DataFrame, interval: str, now: datetime | None = None) -> pd.DataFrame:
    """Remove the candle that is still forming (its close time is in the future)."""
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    close_times = df.index + interval_to_timedelta(interval)
    return df[close_times <= now]


class TwelveDataFeed:
    def __init__(self, api_key: str, symbol: str, session: requests.Session | None = None):
        if not api_key:
            raise DataFeedError("TWELVE_DATA_API_KEY is not set")
        self.api_key = api_key
        self.symbol = symbol
        self.http = session or requests.Session()

    def _request(self, params: dict) -> list[dict]:
        params = {**params, "symbol": self.symbol, "apikey": self.api_key, "timezone": "UTC", "order": "ASC"}
        for attempt in range(3):
            try:
                resp = self.http.get(TWELVE_DATA_URL, params=params, timeout=30)
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                log.warning("Twelve Data request failed (%s), retrying", exc)
                time.sleep(5 * (attempt + 1))
                continue
            if data.get("status") == "error":
                # 429 = per-minute credit limit on the free plan; wait it out.
                if data.get("code") == 429 and attempt < 2:
                    time.sleep(61)
                    continue
                raise DataFeedError(f"Twelve Data error {data.get('code')}: {data.get('message')}")
            return data.get("values", [])
        raise DataFeedError("Twelve Data unreachable after 3 attempts")

    def candles(self, interval: str, outputsize: int = 500) -> pd.DataFrame:
        """Most recent closed candles."""
        values = self._request({"interval": interval, "outputsize": min(outputsize, MAX_OUTPUTSIZE)})
        return drop_incomplete(_to_frame(values), interval)

    def history(self, interval: str, start: str, end: str | None = None) -> pd.DataFrame:
        """Download a long history by paging backwards (used by the backtester)."""
        start_ts = pd.Timestamp(start, tz="UTC")
        end_ts = pd.Timestamp(end, tz="UTC") if end else pd.Timestamp.now(tz="UTC")
        frames = []
        cursor = end_ts
        while cursor > start_ts:
            try:
                values = self._request(
                    {
                        "interval": interval,
                        "outputsize": MAX_OUTPUTSIZE,
                        "start_date": start_ts.strftime("%Y-%m-%d %H:%M:%S"),
                        "end_date": cursor.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
            except DataFeedError as exc:
                # The provider's history ended before `start`; keep what we already have.
                if "No data is available" not in str(exc) or not frames:
                    raise
                log.warning("No %s data before %s; using the history downloaded so far", interval, cursor)
                break
            chunk = _to_frame(values)
            if chunk.empty:
                break
            frames.append(chunk)
            log.info("Downloaded %d %s candles back to %s", len(chunk), interval, chunk.index[0])
            if chunk.index[0] >= cursor:
                break
            cursor = chunk.index[0] - pd.Timedelta(seconds=1)
            time.sleep(8)  # free plan: 8 requests per minute
        if not frames:
            return _to_frame([])
        df = pd.concat(frames).sort_index()
        return drop_incomplete(df[~df.index.duplicated(keep="last")], interval)


class MT5Feed:
    """Live candles from a running MetaTrader 5 terminal (Windows only, `pip install MetaTrader5`)."""

    _TIMEFRAMES = {"1min": "TIMEFRAME_M1", "5min": "TIMEFRAME_M5", "15min": "TIMEFRAME_M15",
                   "30min": "TIMEFRAME_M30", "1h": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4", "1day": "TIMEFRAME_D1"}

    def __init__(self, symbol: str = "XAUUSD"):
        import MetaTrader5 as mt5  # noqa: N813

        if not mt5.initialize():
            raise DataFeedError(f"MT5 initialize failed: {mt5.last_error()}")
        self.mt5 = mt5
        self.symbol = symbol

    def candles(self, interval: str, outputsize: int = 500) -> pd.DataFrame:
        tf = getattr(self.mt5, self._TIMEFRAMES[interval])
        rates = self.mt5.copy_rates_from_pos(self.symbol, tf, 0, outputsize)
        if rates is None:
            raise DataFeedError(f"MT5 returned no data: {self.mt5.last_error()}")
        df = pd.DataFrame(rates)
        # MT5 timestamps are in the broker's server time; most gold brokers use UTC+2/+3.
        # Set MT5_SERVER_UTC_OFFSET_HOURS to your broker's offset.
        offset = pd.Timedelta(hours=float(os.getenv("MT5_SERVER_UTC_OFFSET_HOURS", "0")))
        df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True) - offset
        df = df.set_index("datetime")[["open", "high", "low", "close"]].astype(float)
        return drop_incomplete(df, interval)


def load_csv(path: str) -> pd.DataFrame:
    """CSV with columns datetime,open,high,low,close (datetime in UTC, candle open time)."""
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.set_index("datetime").sort_index()
    return df[["open", "high", "low", "close"]].astype(float)
