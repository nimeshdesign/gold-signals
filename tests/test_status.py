import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import db
from app.engine import Engine
from app.strategies import build_data
from app.telegram_bot import TelegramClient


class QuietNews:
    def blocking_event(self, now=None):
        return None


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "status.db"
    monkeypatch.setattr(db, "settings", SimpleNamespace(database_path=path))
    db.init_db(path)
    return path


def _m15(days=40):
    rng = np.random.default_rng(5)
    n = 96 * days
    end = pd.Timestamp.now(tz="UTC").floor("15min") - pd.Timedelta(minutes=15)
    idx = pd.date_range(end=end, periods=n, freq="15min")
    close = 4100 + np.cumsum(rng.normal(0, 2.0, n))
    df = pd.DataFrame({"open": np.r_[close[0], close[:-1]], "close": close}, index=idx)
    df["high"] = df[["open", "close"]].max(axis=1) + 1
    df["low"] = df[["open", "close"]].min(axis=1) - 1
    return df[["open", "high", "low", "close"]]


def test_engine_saves_status_snapshot(tmp_db):
    m15 = _m15()

    class Feed:
        def candles(self, interval, size):
            return m15 if interval == "15min" else m15.resample("1h").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()

    eng = Engine(feed=Feed(), telegram=TelegramClient("", dry_run=True), news=QuietNews())
    eng.strategy_name = "session_breakout"
    eng.strategy_params = {"range_start": 0, "range_end": 6, "window_end": 16, "trend_tf": "1day",
                           "trend_len": 20, "range_frac": 1.0, "atr_cap": 3}
    eng.run_cycle()
    with db.session(tmp_db) as conn:
        snap = json.loads(db.kv_get(conn, "engine_status"))
        assert db.kv_get(conn, "engine_heartbeat")
    assert snap["price"] == pytest.approx(round(m15["close"].iloc[-1], 2))
    assert snap["trend"]["direction"] in ("UP", "DOWN")
    assert set(snap["range"]) >= {"high", "low", "complete", "window_open"}
    assert "signal_today" in snap


def test_daily_updates_sent_once_each(tmp_db, monkeypatch):
    from app import engine as engine_mod

    monkeypatch.setattr(engine_mod, "settings", SimpleNamespace(**{**vars(engine_mod.settings),
                                                                   "news_filter_enabled": False}))
    sent = []

    class FakeTG:
        dry_run = False

        def send_message(self, chat, text, reply_to=None):
            sent.append(text)
            return len(sent)

    eng = Engine(feed=object(), telegram=FakeTG(), news=QuietNews())
    snap = {"price": 4116.31, "trend": {"direction": "DOWN", "close": 4109.71, "ema": 4214.13},
            "range": {"start": 0, "end": 6, "window_end": 16, "high": 4142.51, "low": 4105.59},
            "window_moves": {"high": 4122.62, "low": 4115.06, "close_high": 4120.8, "close_low": 4116.31},
            "skip_note": None}
    friday = pd.Timestamp("2026-10-09", tz="UTC")

    def at(offset):
        with db.session(tmp_db) as conn:
            eng.send_daily_updates(conn, snap, friday + offset)
        eng.flush_outbox()

    at(pd.Timedelta(hours=5))   # before the range ends: nothing
    assert sent == []
    at(pd.Timedelta(hours=6, minutes=1))
    at(pd.Timedelta(hours=9))   # same day: no repeat
    assert len(sent) == 1 and "daily plan" in sent[0]
    assert "SELL" in sent[0] and "below <code>4,105.59</code>" in sent[0] and "9:30 PM IST" in sent[0]
    at(pd.Timedelta(hours=16, minutes=1))
    at(pd.Timedelta(hours=17))
    assert len(sent) == 3  # day end + Friday weekly summary, once
    assert "No signal today" in sent[1] and "4,115.06 and 4,122.62" in sent[1]
    assert "week of 05 Oct" in sent[2] and "No signals this week" in sent[2]
    at(pd.Timedelta(days=1, hours=7))  # Saturday: nothing more
    assert len(sent) == 3


def test_weekly_summary_caught_up_on_the_weekend(tmp_db):
    """If Friday's day-end never ran (engine down, holiday), the summary still goes out on Saturday."""
    sent = []

    class FakeTG:
        dry_run = False

        def send_message(self, chat, text, reply_to=None):
            sent.append(text)
            return len(sent)

    eng = Engine(feed=object(), telegram=FakeTG(), news=QuietNews())
    with db.session(tmp_db) as conn:
        eng.send_weekly(conn, pd.Timestamp("2026-10-10 09:00", tz="UTC"))  # Saturday
        eng.send_weekly(conn, pd.Timestamp("2026-10-11 09:00", tz="UTC"))  # Sunday: already sent
    eng.flush_outbox()
    assert len(sent) == 1 and "week of 05 Oct" in sent[0]


def test_status_page_renders_empty_and_with_data(tmp_db, sign_in):
    from app.web.main import app

    from app.web import auth, main

    with TestClient(app) as client:
        sign_in(client)
        r = client.get("/status")
        assert r.status_code == 200
        assert "Waiting for first check" in r.text and "No signals yet" in r.text

        now = pd.Timestamp.now(tz="UTC")
        snap = {"updated": now.isoformat(), "price": 4100.5, "price_time": now.isoformat(), "strategy": "session_breakout",
                "params": {}, "tp1_r": 1.0, "tp2_r": 3.0,
                "trend": {"label": "1day close vs 20 EMA", "close": 4090.0, "ema": 4120.0, "direction": "DOWN"},
                "range": {"start": 0, "end": 6, "window_end": 16, "high": 4130.0, "low": 4100.0,
                          "complete": True, "window_open": True},
                "signal_today": {"time": now.isoformat(), "direction": "SELL", "price": 4099.0},
                "skip_note": None, "news_block": None}
        with db.session(tmp_db) as conn:
            db.kv_set(conn, "engine_status", json.dumps(snap))
            db.kv_set(conn, "engine_heartbeat", now.isoformat())
            db.kv_set(conn, "engine_last_error", json.dumps({"time": now.isoformat(), "message": "boom"}))
            sid = db.insert_signal(conn, symbol="XAUUSD", direction="SELL", entry=4099.0, sl=4130.0, tp1=4068.0,
                                   tp2=4006.0, bar_time=now.isoformat(), created_at=now.isoformat())
            db.set_signal_message(conn, sid, 42)
        r = client.get("/status")
        assert r.status_code == 200
        for text in ("Running", "4100.00 – 4130.00", "sells only", "SELL at 4099.00", "boom", "Waiting for TP1 or SL"):
            assert text in r.text, text
