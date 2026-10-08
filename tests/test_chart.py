from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import db
from app.strategies import plan_levels
from app.web import auth, chart

PARAMS = {"range_start": 0, "range_end": 6, "window_end": 16, "trend_tf": "1day", "trend_len": 20,
          "range_frac": 1.0, "atr_cap": 3}
DAY = date(2026, 3, 4)  # a Wednesday


def _candles(days=60):
    rng = np.random.default_rng(11)
    idx = pd.date_range(pd.Timestamp(DAY, tz="UTC") - pd.Timedelta(days=days - 3), periods=96 * days, freq="15min")
    idx = idx[idx.weekday < 5]
    close = 3000 + np.cumsum(rng.normal(0, 1.5, len(idx)))
    df = pd.DataFrame({"open": np.r_[close[0], close[:-1]], "close": close}, index=idx)
    df["high"] = df[["open", "close"]].max(axis=1) + 1
    df["low"] = df[["open", "close"]].min(axis=1) - 1
    return df[["open", "high", "low", "close"]]


@pytest.fixture
def conn_with_data(tmp_path):
    path = tmp_path / "c.db"
    db.init_db(path)
    m15 = _candles()
    with db.session(path) as conn:
        db.upsert_candles(conn, m15)
        # A SELL at 09:00 UTC whose stop sits just above the 09:15 candle's high -> stop hit later or TP.
        entry_bar = pd.Timestamp(DAY, tz="UTC") + pd.Timedelta(hours=9)
        entry = float(m15.loc[entry_bar, "close"])
        sid = db.insert_signal(conn, symbol="XAUUSD", direction="SELL", entry=round(entry, 2),
                               sl=round(entry + 5, 2), tp1=round(entry - 5, 2), tp2=round(entry - 15, 2),
                               bar_time=entry_bar.isoformat(), created_at=(entry_bar + pd.Timedelta(minutes=15)).isoformat())
    return path, m15, sid


def test_candle_storage_roundtrip(conn_with_data):
    path, m15, _ = conn_with_data
    with db.session(path) as conn:
        back = db.load_candles(conn, m15.index[0].isoformat(), (m15.index[-1] + pd.Timedelta(minutes=1)).isoformat())
        days = db.candle_days(conn)
        db.upsert_candles(conn, m15.tail(5))  # re-saving the same candles doesn't duplicate them
        assert db.candle_count(conn) == len(m15)
    assert len(back) == len(m15)
    assert back["close"].iloc[-1] == pytest.approx(m15["close"].iloc[-1])
    assert DAY.isoformat() in days and all(pd.Timestamp(d).weekday() < 5 for d in days)


def test_day_view_levels_and_replay(conn_with_data):
    path, m15, sid = conn_with_data
    with db.session(path) as conn:
        v = chart.day_view(conn, DAY, PARAMS, 1.0, 3.0, 5.5)
    day = m15[m15.index.normalize() == pd.Timestamp(DAY, tz="UTC")]
    asian = day[day.index.hour < 6]
    assert v["range"]["high"] == pytest.approx(round(asian["high"].max(), 2))
    assert v["range"]["low"] == pytest.approx(round(asian["low"].min(), 2))
    assert v["trend"]["direction"] in ("UP", "DOWN")
    titles = {lvl["title"] for lvl in v["levels"]}
    assert {"Range high", "Range low", "Entry SELL", "Stop loss", "TP1", "TP2"} <= titles
    assert len(v["trades"]) == 1 and v["trades"][0]["row"]["id"] == sid
    events = [e["event"] for e in v["trades"][0]["events"]]
    assert events and events[-1] in ("sl", "be", "tp2")
    times = [m["time"] for m in v["markers"]]
    assert times == sorted(times)
    # chart times are shifted to IST (UTC + 5:30)
    first = pd.Timestamp(DAY, tz="UTC")
    assert v["candles"][0][0] == int((first + pd.Timedelta(hours=5.5)).timestamp())


def test_day_without_signal_shows_trigger(conn_with_data):
    path, _, _ = conn_with_data
    with db.session(path) as conn:
        v = chart.day_view(conn, date(2026, 3, 3), PARAMS, 1.0, 3.0, 5.5)
    assert not v["trades"]
    assert any(lvl["kind"] == "trigger" for lvl in v["levels"]) == (v["trigger"] is not None)


def test_planned_levels_match_strategy_stop():
    idx = pd.date_range("2026-03-04", periods=96, freq="15min", tz="UTC")
    signals = pd.DataFrame({"signal": 0, "sl_dist": 10.0}, index=idx)
    signals.loc[idx[-1], "sl_dist"] = 12.0  # latest candle in the window decides (16:00 is outside -> 15:45)
    start = pd.Timestamp("2026-03-04", tz="UTC")
    sell = plan_levels(signals, start, {"side": "SELL", "price": 3000.0}, PARAMS, 1.0, 3.0)
    assert sell == {"side": "SELL", "entry": 3000.0, "risk": 10.0, "sl": 3010.0, "tp1": 2990.0, "tp2": 2970.0}
    buy = plan_levels(signals, start, {"side": "BUY", "price": 3000.0}, PARAMS, 1.5, 3.0)
    assert (buy["sl"], buy["tp1"], buy["tp2"]) == (2990.0, 3015.0, 3030.0)
    assert plan_levels(signals, start, None, PARAMS, 1.0, 3.0) is None


def test_day_without_signal_draws_planned_levels(conn_with_data):
    path, _, _ = conn_with_data
    with db.session(path) as conn:
        v = chart.day_view(conn, date(2026, 3, 3), PARAMS, 1.0, 3.0, 5.5)
    assert v["trigger"] is not None, "57 days of candles is enough history for the 20-day trend"
    kinds = [lvl["kind"] for lvl in v["levels"]]
    assert kinds.count("plan_sl") == 1 and kinds.count("plan_tp") == 2
    p = v["planned"]
    assert p["entry"] == v["trigger"]["price"]
    assert abs(p["tp1"] - p["entry"]) == pytest.approx(p["risk"], abs=0.02)


def test_daily_plan_message_includes_planned_levels():
    from app.telegram_bot import format_daily_plan

    snap = {"price": 4116.31, "trend": {"direction": "DOWN", "close": 4109.71, "ema": 4214.13},
            "range": {"high": 4142.51, "low": 4105.59, "window_end": 16},
            "planned": {"side": "SELL", "entry": 4105.59, "risk": 36.92, "sl": 4142.51, "tp1": 4068.67, "tp2": 3994.83}}
    text = format_daily_plan("XAUUSD", snap, pd.Timestamp("2026-10-08 06:01", tz="UTC"), [])
    assert "SL ~<code>4,142.51</code>" in text and "TP1 ~<code>4,068.67</code>" in text and "TP2 ~<code>3,994.83</code>" in text


def test_signal_message_shows_pips_and_lot_money(monkeypatch):
    from app import telegram_bot
    from app.config import settings

    monkeypatch.setattr(telegram_bot, "_settings", lambda: settings)
    import app.config as cfg

    monkeypatch.setattr(cfg, "settings", SimpleNamespace(**{**vars(settings), "pip_size": 0.10,
                                                            "contract_oz": 100, "lot_size": 0.20}))
    text = telegram_bot.format_signal("XAUUSD", "SELL", 4105.59, 4115.59, 4095.59, 4075.59, 1.0, 3.0)
    assert "SL: <code>4,115.59</code> (100 pips)" in text
    assert "TP1: <code>4,095.59</code> (100 pips, 1R)" in text
    assert "TP2: <code>4,075.59</code> (300 pips, 3R)" in text
    assert "At 0.2 lot: risk $200 · TP1 +$200 · TP2 +$600" in text


def test_fixed_pip_stop_overrides_range_stop():
    from app.strategies import build_data, compute_signals

    m15 = _candles(40)
    s = compute_signals("session_breakout", build_data(m15), {**PARAMS, "sl_fixed": 10.0})
    fired = s[s["signal"] != 0]
    assert len(fired) and (fired["sl_dist"] == 10.0).all()


def test_weekend_has_no_view(conn_with_data):
    path, _, _ = conn_with_data
    with db.session(path) as conn:
        assert chart.day_view(conn, date(2026, 3, 7), PARAMS, 1.0, 3.0, 5.5) is None


def test_neighbours_skip_weekends():
    days = ["2026-03-05", "2026-03-06", "2026-03-09"]
    assert chart.neighbours(days, date(2026, 3, 6)) == ("2026-03-05", "2026-03-09")
    assert chart.neighbours(days, date(2026, 3, 5)) == (None, "2026-03-06")


def test_chart_page(conn_with_data, monkeypatch, sign_in):
    path, _, sid = conn_with_data
    from app.web import main

    monkeypatch.setattr(db, "settings", SimpleNamespace(database_path=path))
    monkeypatch.setattr(main, "settings", SimpleNamespace(**{**vars(main.settings), "database_path": path,
                                                             "strategy_params": PARAMS}))
    with TestClient(main.app) as client:
        assert client.get("/chart", follow_redirects=False).status_code == 303  # login required
        sign_in(client)
        r = client.get(f"/chart?signal={sid}")
        assert r.status_code == 200
        for text in ("Wednesday 04 March 2026", "Signal #", "Stop loss", "lightweight-charts", "Range high"):
            assert text in r.text, text
        r = client.get("/chart?day=2026-03-07")  # a Saturday
        assert r.status_code == 200 and "No candles for this day" in r.text


def test_engine_backfills_candles_once(tmp_path, monkeypatch):
    from app.engine import Engine
    from app.telegram_bot import TelegramClient

    path = tmp_path / "e.db"
    db.init_db(path)
    monkeypatch.setattr(db, "settings", SimpleNamespace(database_path=path))
    m15 = _candles(80)
    calls = []

    class Feed:
        def candles(self, interval, size):
            calls.append((interval, size))
            return m15.tail(size)

    from app import engine as engine_mod

    monkeypatch.setattr(engine_mod.market, "is_open", lambda ts: True)
    monkeypatch.setattr(engine_mod, "settings", SimpleNamespace(**{**vars(engine_mod.settings),
                                                                   "news_filter_enabled": False, "daily_updates": False,
                                                                   "extra_strategies": []}))
    hourly = m15.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    Feed.candles = lambda self, interval, size: (calls.append((interval, size)),
                                                 hourly if interval == "1h" else m15.tail(size))[1]
    eng = Engine(feed=Feed(), telegram=TelegramClient("", dry_run=True), news=SimpleNamespace(blocking_event=lambda: None))
    eng.run_cycle()
    eng.run_cycle()
    with db.session(path) as conn:
        assert db.candle_count(conn) == min(len(m15), 5000)
    assert calls.count(("15min", 5000)) == 1  # backfilled only the first time
    assert calls.count(("1h", 5000)) == 1     # hourly candles fetched once per hour, not every cycle
