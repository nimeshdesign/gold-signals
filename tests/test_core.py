import numpy as np
import pandas as pd
import pytest

from app import db
from app.config import StrategyParams
from app.indicators import atr, ema, rsi
from app.news import parse_events
from app.outcome import BREAKEVEN, STOPPED, TARGET, TP1, TradeState, expire, step
from app.strategy import build_frame, interval_to_timedelta, latest_signal
from app.web.stats import compute_stats


# ---------- indicators ----------

def test_ema_matches_recursive_definition():
    s = pd.Series([1.0, 2, 3, 4, 5])
    out = ema(s, 3)
    k = 2 / 4
    expected = [1.0]
    for x in s[1:]:
        expected.append(x * k + expected[-1] * (1 - k))
    assert np.allclose(out, expected)


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert rsi(up, 14).iloc[-1] == 100
    down = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert rsi(down, 14).iloc[-1] == pytest.approx(0)
    noisy = pd.Series(np.random.default_rng(0).normal(0, 1, 300).cumsum() + 100)
    r = rsi(noisy, 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_atr_constant_range():
    df = pd.DataFrame({"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0}, index=range(30))
    assert atr(df, 14).iloc[-1] == pytest.approx(2.0)


def test_interval_parsing():
    assert interval_to_timedelta("15min") == pd.Timedelta(minutes=15)
    assert interval_to_timedelta("4h") == pd.Timedelta(hours=4)
    assert interval_to_timedelta("1day") == pd.Timedelta(days=1)


# ---------- trade management ----------

def buy():
    return TradeState("BUY", entry=100, sl=90, tp1=115, tp2=130)


def test_stop_loss():
    st = buy()
    assert step(st, high=105, low=89) == [STOPPED]
    assert st.result_r == -1


def test_same_candle_stop_and_target_counts_stop():
    st = buy()
    step(st, high=120, low=85)
    assert st.status == STOPPED


def test_tp1_then_breakeven():
    st = buy()
    assert step(st, 116, 101) == [TP1]
    assert step(st, 110, 99.5) == [BREAKEVEN]
    assert st.result_r == 0.75


def test_tp1_then_tp2():
    st = buy()
    step(st, 116, 101)
    assert step(st, 131, 105) == [TARGET]
    assert st.result_r == 2.25


def test_sell_side():
    st = TradeState("SELL", entry=100, sl=110, tp1=85, tp2=70)
    step(st, 99, 84)
    step(st, 90, 69)
    assert st.status == TARGET and st.result_r == 2.25


def test_expiry_after_tp1():
    st = buy()
    step(st, 116, 101)
    expire(st, 120)  # half at +1.5R, half at +2R
    assert st.result_r == pytest.approx(1.75)


def test_closed_trade_ignores_candles():
    st = buy()
    step(st, 100, 80)
    assert step(st, 200, 150) == []


# ---------- strategy ----------

def _frames(trend_up: bool):
    """Long uptrend (or downtrend), then a pullback and a resumption that forces an EMA cross."""
    n = 2400
    idx = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    sign = 1 if trend_up else -1
    base = 2000 + sign * np.linspace(0, 400, n)
    # pullback over bars 2300-2370, then resume
    wiggle = np.zeros(n)
    wiggle[2300:2370] = -sign * np.linspace(0, 25, 70)
    wiggle[2370:] = -sign * 25 + sign * np.linspace(0, 40, n - 2370)
    close = base + wiggle
    ltf = pd.DataFrame({"open": close, "high": close + 1.5, "low": close - 1.5, "close": close}, index=idx)
    htf = ltf.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    return htf, ltf


@pytest.mark.parametrize("trend_up,expected", [(True, 1), (False, -1)])
def test_crossover_with_trend_fires(trend_up, expected):
    htf, ltf = _frames(trend_up)
    params = StrategyParams(trend_ema=50, rsi_buy_max=100, rsi_sell_min=0)
    frame = build_frame(htf, ltf, params, "4h", "15min")
    fired = frame[frame["signal"] != 0]["signal"]
    assert len(fired) >= 1
    assert set(fired) == {expected}


def test_signal_levels():
    htf, ltf = _frames(True)
    params = StrategyParams(trend_ema=50, rsi_buy_max=100)
    frame = build_frame(htf, ltf, params, "4h", "15min")
    first = frame.index[frame["signal"] == 1][0]
    sig = latest_signal(htf, ltf.loc[:first], params, "4h", "15min")
    assert sig is not None and sig.direction == "BUY"
    assert sig.sl < sig.entry < sig.tp1 < sig.tp2
    assert sig.tp1 - sig.entry == pytest.approx(1.5 * sig.risk, abs=0.02)
    assert sig.entry_time == first + pd.Timedelta(minutes=15)


def test_htf_trend_has_no_lookahead():
    """A 15-min candle must not see the 4h candle that is still forming."""
    htf, ltf = _frames(True)
    params = StrategyParams(trend_ema=5)
    # Make the last 4h candle hugely bearish; it should only affect LTF candles closing at/after its close.
    htf.iloc[-1, htf.columns.get_loc("close")] = 0
    frame = build_frame(htf, ltf, params, "4h", "15min")
    last_htf_open = htf.index[-1]
    inside = frame[(frame.index >= last_htf_open)]
    # Every 15-min candle closing before the 4h candle closes still sees the old trend...
    assert (inside["trend"].iloc[:-1] == 1).all()
    # ...and the one closing at the same moment sees the new one.
    assert inside["trend"].iloc[-1] == -1


# ---------- news ----------

def test_news_parsing_filters_usd_high():
    raw = [
        {"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High"},
        {"title": "Retail Sales", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "Medium"},
        {"title": "ECB", "country": "EUR", "date": "2026-10-14T08:30:00-04:00", "impact": "High"},
    ]
    events = parse_events(raw)
    assert [e.title for e in events] == ["CPI m/m"]
    assert events[0].time.hour == 12  # converted to UTC


# ---------- gaps ----------

def test_stop_gapped_through_fills_at_the_open():
    st = buy()  # entry 100, SL 90
    step(st, high=89, low=85, open_=87)  # opens 3 below the stop
    assert st.status == STOPPED and st.result_r == -1.3


def test_breakeven_gap_fills_at_the_open():
    st = buy()  # entry 100, TP1 115
    step(st, 116, 101)
    step(st, 99, 95, open_=96)  # opens 4 below entry after TP1
    assert st.status == BREAKEVEN and st.result_r == pytest.approx(0.75 - 0.2)


def test_normal_stop_unchanged_without_gap():
    st = buy()
    step(st, high=101, low=89, open_=100)
    assert st.result_r == -1.0


# ---------- db + stats ----------

def test_db_roundtrip_and_stats(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    with db.session(path) as conn:
        for i, (res, closed) in enumerate([(-1.0, "2026-01-01T10:00:00+00:00"), (2.25, "2026-01-02T10:00:00+00:00"),
                                           (0.75, "2026-01-03T10:00:00+00:00")]):
            sid = db.insert_signal(conn, symbol="XAUUSD", direction="BUY", entry=1, sl=0, tp1=2, tp2=3,
                                   bar_time=f"2026-01-0{i + 1}T00:00:00+00:00", created_at="x")
            db.update_signal(conn, sid, status="tp2", result_r=res, closed_at=closed)
        # duplicate candle is ignored
        assert db.insert_signal(conn, symbol="XAUUSD", direction="BUY", entry=1, sl=0, tp1=2, tp2=3,
                                bar_time="2026-01-01T00:00:00+00:00", created_at="x") is None
        stats = compute_stats(db.all_signals(conn))
    assert stats.closed == 3 and stats.wins == 2 and stats.losses == 1
    assert stats.total_r == 2.0
    assert stats.profit_factor == 3.0
    assert stats.max_drawdown_r == 1.0
    assert stats.equity[-1][1] == 2.0


# ---------- telegram ----------

def test_telegram_errors_carry_retry_after():
    from app.telegram_bot import TelegramClient, TelegramError

    class FakeResp:
        def json(self):
            return {"ok": False, "description": "Too Many Requests: retry after 7", "parameters": {"retry_after": 7}}

    class FakeSession:
        def post(self, url, json, timeout):
            return FakeResp()

    with pytest.raises(TelegramError) as err:
        TelegramClient("t", session=FakeSession()).send_message("@c", "hi")
    assert err.value.retry_after == 7


def test_telegram_network_error_becomes_telegram_error():
    import requests

    from app.telegram_bot import TelegramClient, TelegramError

    class FakeSession:
        def post(self, url, json, timeout):
            raise requests.ConnectionError("down")

    with pytest.raises(TelegramError):
        TelegramClient("t", session=FakeSession()).send_message("@c", "hi")
