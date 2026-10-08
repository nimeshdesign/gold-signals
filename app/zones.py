"""Multi-timeframe zone strategy with 1-minute confirmation.

Levels: swing highs/lows ("pivots") on 1-hour, 15-minute and 5-minute candles built from 1-minute data,
plus the previous day's high/low. A pivot only counts once it is confirmed (`right` candles later), so
nothing here looks into the future.

Zones: a band of +/- zone_pips around each level. Levels from several timeframes that sit inside the same
band add up to a stronger zone (score = number of distinct timeframes).

Trade: in the trend direction only. Price touches a zone (support for buys, resistance for sells); within
`confirm_bars` minutes a 1-minute candle must close back outside the zone, in the trade direction, and
beyond the previous candle's high (buy) / low (sell). Entry at that candle's close. One trade per zone.

Output follows the same contract as app/strategies.py, on 1-minute candles:
    signal (+1/-1/0), close, close_time, sl_dist, plus zone info for messages.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .indicators import ema
from .strategies import OHLC

M1 = pd.Timedelta(minutes=1)
TIMEFRAMES = {"1h": ("1h", 72), "15m": ("15min", 48), "5m": ("5min", 12)}  # label: (rule, lookback hours)

DEFAULT_ZONE_PARAMS = {
    "pivot_left": 3, "pivot_right": 3,      # candles on each side that a swing point must beat
    "zone_pips": 15,                        # zone half-width
    "min_score": 2,                         # distinct timeframes (incl. previous-day H/L) in the zone
    "confirm_bars": 5,                      # minutes allowed between touch and confirmation candle
    "session_start": 7, "session_end": 17,  # UTC hours (12:30 PM – 10:30 PM IST)
    "max_per_day": 2,
    "trend": "1h_ema50",                    # "1h_ema50", "daily_ema20" or "none"
    "sl_mode": "zone",                      # "zone" = beyond the zone edge, or "fixed"
    "sl_buffer_pips": 10, "sl_min_pips": 40, "sl_max_pips": 150,
    "sl_fixed_pips": 100,
    "pip": 0.10,
}


def resample(m1: pd.DataFrame, rule: str) -> pd.DataFrame:
    return m1.resample(rule, label="left", closed="left").agg(OHLC).dropna()


def pivots(df: pd.DataFrame, bar: pd.Timedelta, left: int, right: int) -> pd.DataFrame:
    """Swing highs/lows with the time they become known (close of the `right`-th candle after)."""
    hi, lo = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    rows = []
    for i in range(left, n - right):
        win_hi = hi[i - left:i + right + 1]
        win_lo = lo[i - left:i + right + 1]
        known = df.index[i + right] + bar
        if hi[i] == win_hi.max() and (win_hi == hi[i]).sum() == 1:
            rows.append((known, df.index[i], float(hi[i]), "high"))
        if lo[i] == win_lo.min() and (win_lo == lo[i]).sum() == 1:
            rows.append((known, df.index[i], float(lo[i]), "low"))
    return pd.DataFrame(rows, columns=["known", "at", "price", "kind"]).sort_values("known", ignore_index=True)


@dataclass
class Level:
    known: pd.Timestamp
    at: pd.Timestamp
    price: float
    kind: str      # "high" (resistance) or "low" (support)
    tf: str        # "1h", "15m", "5m" or "pdh"/"pdl"
    expires: pd.Timestamp


def build_levels(m1: pd.DataFrame, p: dict) -> list[Level]:
    out: list[Level] = []
    for tf, (rule, hours) in TIMEFRAMES.items():
        bars = resample(m1, rule)
        for r in pivots(bars, pd.Timedelta(rule), p["pivot_left"], p["pivot_right"]).itertuples():
            out.append(Level(r.known, r.at, r.price, r.kind, tf, r.known + pd.Timedelta(hours=hours)))
    daily = resample(m1, "1D")
    for i in range(1, len(daily)):
        day = daily.index[i]
        prev = daily.iloc[i - 1]
        out.append(Level(day, daily.index[i - 1], float(prev["high"]), "high", "pdh", day + pd.Timedelta(days=1)))
        out.append(Level(day, daily.index[i - 1], float(prev["low"]), "low", "pdl", day + pd.Timedelta(days=1)))
    out.sort(key=lambda lv: lv.known)
    return out


def trend_series(m1: pd.DataFrame, mode: str) -> pd.Series:
    """+1 up / -1 down / 0 unknown for each 1-minute candle, from CLOSED higher-timeframe candles only."""
    rule, length = ("1h", 50) if mode == "1h_ema50" else ("1D", 20)
    htf = resample(m1, rule)
    sign = np.sign(htf["close"] - ema(htf["close"], length))
    sign.iloc[:length] = 0
    known = pd.Series(sign.to_numpy(), index=htf.index + pd.Timedelta(rule))  # value usable from candle close
    return known.reindex(m1.index + M1, method="ffill").fillna(0).set_axis(m1.index)


def compute_zone_signals(m1: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = {**DEFAULT_ZONE_PARAMS, **(params or {})}
    pip = p["pip"]
    zw = p["zone_pips"] * pip
    levels = build_levels(m1, p)
    if p["trend"] == "none":
        trend = None
    else:
        trend = trend_series(m1, p["trend"]).to_numpy()

    idx = m1.index
    o, h, l, c = (m1[k].to_numpy() for k in ("open", "high", "low", "close"))
    n = len(m1)
    signal = np.zeros(n, dtype=int)
    sl_dist = np.full(n, np.nan)
    zone_lo = np.full(n, np.nan)
    zone_hi = np.full(n, np.nan)
    zone_score = np.zeros(n, dtype=int)
    zone_tfs = np.empty(n, dtype=object)

    active: list[Level] = []
    nxt = 0
    used: set[tuple] = set()
    touched: dict[tuple, int] = {}   # zone key -> bar index of the touch
    day_count: dict = {}

    for i in range(1, n):
        t_close = idx[i] + M1
        while nxt < len(levels) and levels[nxt].known <= idx[i]:
            active.append(levels[nxt])
            nxt += 1
        if i % 60 == 0 or not active:
            active = [lv for lv in active if lv.expires > idx[i]]
        hour = idx[i].hour
        if not (p["session_start"] <= hour < p["session_end"]) or idx[i].weekday() >= 5:
            continue
        day = idx[i].date()
        if day_count.get(day, 0) >= p["max_per_day"]:
            continue

        for side, kind in ((1, "low"), (-1, "high")):
            if trend is not None and trend[i] != side:
                continue
            # Cluster levels of this kind near current price into zones.
            near = [lv for lv in active if lv.kind == kind and abs(lv.price - c[i]) <= 6 * zw]
            if not near:
                continue
            for anchor in near:
                members = [lv for lv in near if abs(lv.price - anchor.price) <= zw]
                tfs = {lv.tf if lv.tf in TIMEFRAMES else "day" for lv in members}
                if len(tfs) < p["min_score"]:
                    continue
                prices = [lv.price for lv in members]
                z_lo, z_hi = min(prices) - zw, max(prices) + zw
                key = (kind, round((z_lo + z_hi) / 2 / (2 * zw)))
                if key in used:
                    continue
                # Zone broken: close through the far side -> retire it.
                if (side == 1 and c[i] < z_lo) or (side == -1 and c[i] > z_hi):
                    used.add(key)
                    touched.pop(key, None)
                    continue
                touching = (l[i] <= z_hi and h[i] >= z_lo)
                if touching and key not in touched:
                    touched[key] = i
                if key not in touched:
                    continue
                if i - touched[key] > p["confirm_bars"]:
                    touched.pop(key, None)
                    continue
                # 1-minute confirmation candle.
                if side == 1:
                    ok = c[i] > z_hi and c[i] > o[i] and c[i] > h[i - 1]
                else:
                    ok = c[i] < z_lo and c[i] < o[i] and c[i] < l[i - 1]
                if not ok:
                    continue
                if p["sl_mode"] == "fixed":
                    risk = p["sl_fixed_pips"] * pip
                else:
                    edge = z_lo - p["sl_buffer_pips"] * pip if side == 1 else z_hi + p["sl_buffer_pips"] * pip
                    risk = float(np.clip(abs(c[i] - edge), p["sl_min_pips"] * pip, p["sl_max_pips"] * pip))
                signal[i], sl_dist[i] = side, risk
                zone_lo[i], zone_hi[i], zone_score[i] = z_lo, z_hi, len(tfs)
                zone_tfs[i] = "+".join(sorted(tfs))
                used.add(key)
                touched.pop(key, None)
                day_count[day] = day_count.get(day, 0) + 1
                break
            if signal[i]:
                break

    return pd.DataFrame({
        "signal": signal, "close": c, "close_time": idx + M1, "sl_dist": sl_dist,
        "zone_lo": zone_lo, "zone_hi": zone_hi, "zone_score": zone_score, "zone_tfs": zone_tfs,
    }, index=idx)
