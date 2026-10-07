import numpy as np
import pandas as pd
import pytest

from app.outcome import TP1
from app.simulator import SimConfig, metrics, simulate
from app.strategies import STRATEGIES, build_data, compute_signals

PARAMS = {
    "ema_cross": {"tf": "15min", "trend_tf": "4h", "trend_len": 20, "fast": 9, "slow": 21, "atr_mult": 1.5},
    "trend_pullback": {"tf": "1h", "trend_tf": "4h", "trend_len": 20, "fast": 10, "slow": 30, "lookback": 3,
                       "atr_mult": 1.5},
    "rsi2_reversion": {"tf": "1h", "trend_tf": "1day", "trend_len": 5, "slow": 20, "rsi_lo": 15, "atr_mult": 2.0},
    "session_breakout": {"range_start": 0, "range_end": 7, "window_end": 16, "trend_tf": "1day", "trend_len": 5,
                         "range_frac": 1.0, "atr_cap": 3},
}


@pytest.fixture(scope="module")
def m15():
    rng = np.random.default_rng(3)
    n = 96 * 60
    idx = pd.date_range("2025-01-06", periods=n, freq="15min", tz="UTC")
    close = 2600 + np.cumsum(rng.normal(0.05, 2.0, n))
    df = pd.DataFrame({"open": np.r_[close[0], close[:-1]], "close": close}, index=idx)
    df["high"] = df[["open", "close"]].max(axis=1) + rng.uniform(0, 1.5, n)
    df["low"] = df[["open", "close"]].min(axis=1) - rng.uniform(0, 1.5, n)
    return df[["open", "high", "low", "close"]]


def test_params_cover_every_strategy():
    assert set(PARAMS) == set(STRATEGIES)


@pytest.mark.parametrize("name", sorted(PARAMS))
def test_no_lookahead(name, m15):
    """The live engine only has candles up to now; its signal must equal the backtest's at that moment."""
    full = compute_signals(name, build_data(m15), PARAMS[name])
    fired = full[full["signal"] != 0]
    assert len(fired) >= 3, "strategy should fire on 60 days of random data"
    for bar_time, row in fired.iloc[:: max(1, len(fired) // 8)].iterrows():
        # Everything the engine would have at the moment this candle closed.
        past = m15[m15.index + pd.Timedelta(minutes=15) <= row["close_time"]]
        live = compute_signals(name, build_data(past), PARAMS[name])
        assert live.index[-1] == bar_time
        assert live["signal"].iloc[-1] == row["signal"]
        assert live["sl_dist"].iloc[-1] == pytest.approx(row["sl_dist"])


def test_sell_spread_is_charged_on_exit():
    """A sell whose target is touched by less than the spread is not a win."""
    idx = pd.date_range("2025-01-06", periods=4, freq="15min", tz="UTC")
    m15 = pd.DataFrame({"open": 100.0, "high": [100, 100.2, 100.2, 100.2], "low": [100, 98.9, 99.8, 99.8],
                        "close": 100.0}, index=idx)
    sig = pd.DataFrame({"signal": [-1], "close": [100.0], "close_time": [idx[0] + pd.Timedelta(minutes=15)],
                        "sl_dist": [1.0]}, index=[idx[0]])
    # TP1 = 99.0. The bid low of 98.9 touches it, but a sell closes at the ask: 98.9 + 0.3 = 99.2, not reached.
    trades = simulate(sig, m15, SimConfig(tp1_r=1.0, tp2_r=2.0, spread=0.3, expiry_hours=0.75))
    assert trades.iloc[0]["outcome"] != TP1 and trades.iloc[0]["result_r"] <= 0


def test_buy_spread_reduces_result():
    idx = pd.date_range("2025-01-06", periods=3, freq="15min", tz="UTC")
    m15 = pd.DataFrame({"open": 100.0, "high": [100, 98.5, 98.5], "low": [100, 98.5, 98.5], "close": 100.0},
                       index=idx)
    sig = pd.DataFrame({"signal": [1], "close": [100.0], "close_time": [idx[0] + pd.Timedelta(minutes=15)],
                        "sl_dist": [1.0]}, index=[idx[0]])
    trades = simulate(sig, m15, SimConfig(spread=0.3))
    assert trades.iloc[0]["result_r"] == pytest.approx(-1.3)
    assert metrics(trades)["win_rate"] == 0
