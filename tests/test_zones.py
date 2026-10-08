import numpy as np
import pandas as pd
import pytest

from app.zones import M1, compute_zone_signals, pivots


@pytest.fixture(scope="module")
def m1():
    rng = np.random.default_rng(21)
    idx = pd.date_range("2026-03-02", periods=60 * 24 * 12, freq="1min", tz="UTC")
    idx = idx[idx.weekday < 5]
    close = 3000 + np.cumsum(rng.normal(0, 0.35, len(idx)))
    df = pd.DataFrame({"open": np.r_[close[0], close[:-1]], "close": close}, index=idx)
    df["high"] = df[["open", "close"]].max(axis=1) + rng.uniform(0, 0.3, len(idx))
    df["low"] = df[["open", "close"]].min(axis=1) - rng.uniform(0, 0.3, len(idx))
    return df[["open", "high", "low", "close"]]


PARAMS = {"trend": "none", "min_score": 1, "max_per_day": 5}


def test_pivots_are_only_known_after_confirmation():
    idx = pd.date_range("2026-03-02", periods=9, freq="1h", tz="UTC")
    highs = [1, 2, 3, 9, 3, 2, 1, 2, 1]
    df = pd.DataFrame({"open": 0, "high": highs, "low": [h - 1 for h in highs], "close": 0}, index=idx)
    p = pivots(df, pd.Timedelta("1h"), 3, 3)
    top = p[p.kind == "high"].iloc[0]
    assert top.price == 9 and top["at"] == idx[3]
    assert top.known == idx[6] + pd.Timedelta("1h")  # three candles later, at that candle's close


def test_zone_signals_fire_and_have_levels(m1):
    s = compute_zone_signals(m1, PARAMS)
    fired = s[s.signal != 0]
    assert len(fired) >= 5
    assert (fired.sl_dist > 0).all()
    buys, sells = fired[fired.signal == 1], fired[fired.signal == -1]
    assert (buys.close > buys.zone_hi).all() and (sells.close < sells.zone_lo).all()
    assert (fired.groupby(fired.index.date).size() <= PARAMS["max_per_day"]).all()


def test_zone_signals_have_no_lookahead(m1):
    """The live engine sees candles only up to now; each signal must match the full-history run."""
    full = compute_zone_signals(m1, PARAMS)
    fired = full[full.signal != 0]
    for t, row in fired.iloc[:: max(1, len(fired) // 6)].iterrows():
        past = m1[m1.index + M1 <= row.close_time]
        live = compute_zone_signals(past, PARAMS)
        assert live.index[-1] == t
        assert live.signal.iloc[-1] == row.signal
        assert live.sl_dist.iloc[-1] == pytest.approx(row.sl_dist)
