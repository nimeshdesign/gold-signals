from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import db
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


def test_weekend_has_no_view(conn_with_data):
    path, _, _ = conn_with_data
    with db.session(path) as conn:
        assert chart.day_view(conn, date(2026, 3, 7), PARAMS, 1.0, 3.0, 5.5) is None


def test_neighbours_skip_weekends():
    days = ["2026-03-05", "2026-03-06", "2026-03-09"]
    assert chart.neighbours(days, date(2026, 3, 6)) == ("2026-03-05", "2026-03-09")
    assert chart.neighbours(days, date(2026, 3, 5)) == (None, "2026-03-06")


def test_chart_page(conn_with_data, monkeypatch):
    path, _, sid = conn_with_data
    from app.web import main

    monkeypatch.setattr(db, "settings", SimpleNamespace(database_path=path))
    monkeypatch.setattr(main, "settings", SimpleNamespace(**{**vars(main.settings), "database_path": path,
                                                             "strategy_params": PARAMS}))
    with TestClient(main.app) as client:
        assert client.get("/chart", follow_redirects=False).status_code == 303  # login required
        client.cookies.set(auth.COOKIE, auth.make_session("admin", main._secret(), 3600))
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

    eng = Engine(feed=Feed(), telegram=TelegramClient("", dry_run=True), news=SimpleNamespace(blocking_event=lambda: None))
    with db.session(path) as conn:
        eng.save_candles(conn, m15.tail(1000))
        eng.save_candles(conn, m15.tail(1000))
        assert db.candle_count(conn) == min(len(m15), 5000)
    assert calls == [("15min", 5000)]  # backfilled only the first time
