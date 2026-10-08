import json
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import db
from app import engine as engine_mod
from app.engine import Engine

ENTRY_TIME = pd.Timestamp("2026-10-08 13:00", tz="UTC")


class QuietNews:
    def blocking_event(self, now=None):
        return None


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(db, "settings", SimpleNamespace(database_path=path))
    monkeypatch.setattr(engine_mod, "settings", SimpleNamespace(**{
        **vars(engine_mod.settings), "live_spread": 0.30, "track_minutes": 1, "track_slow_minutes": 5,
        "track_credit_budget": 3, "extra_strategies": []}))
    db.init_db(path)
    sent = []

    class TG:
        dry_run = False

        def send_message(self, chat, text, reply_to=None):
            sent.append((text, reply_to))
            return 100 + len(sent)

    bars = {}

    class Feed:
        def candles(self, interval, size):
            return bars[interval]

    eng = Engine(feed=Feed(), telegram=TG(), news=QuietNews())
    with db.session(path) as conn:
        sid = db.insert_signal(conn, symbol="XAUUSD", direction="SELL", entry=4117.96, sl=4127.96, tp1=4107.96,
                               tp2=4087.96, bar_time=(ENTRY_TIME - pd.Timedelta(minutes=15)).isoformat(),
                               created_at=ENTRY_TIME.isoformat(),
                               last_checked=(ENTRY_TIME - pd.Timedelta(minutes=1)).isoformat(), strategy="orb", main=False)
        db.set_signal_message(conn, sid, 8)
    return SimpleNamespace(eng=eng, path=path, sent=sent, bars=bars, sid=sid)


def m1(rows):
    idx = pd.DatetimeIndex([ENTRY_TIME + pd.Timedelta(minutes=m) for m, *_ in rows])
    return pd.DataFrame([{"open": o, "high": h, "low": low, "close": c} for _, o, h, low, c in rows], index=idx)


def track(setup):
    """One tracking pass, committed, then the outbox sent (as the engine does)."""
    with db.session(setup.path) as conn:
        setup.eng.track_live(conn)
    setup.eng.flush_outbox()


def test_sell_stop_hit_by_ask_is_announced_within_a_minute(setup):
    # 13:13 bid high 4127.70 + 0.30 spread = 4128.00 >= SL 4127.96 (the broker's ask touched the stop).
    setup.bars["1min"] = m1([(0, 4118, 4119, 4116, 4117), (13, 4125, 4127.70, 4124, 4126)])
    track(setup)
    with db.session(setup.path) as conn:
        row = db.all_signals(conn)[0]
        live = json.loads(db.kv_get(conn, "live_price"))
    assert row["status"] == "sl" and row["result_r"] == -1.0
    assert row["closed_at"] == (ENTRY_TIME + pd.Timedelta(minutes=14)).isoformat()
    text, reply_to = setup.sent[-1]
    assert "stop loss hit" in text and reply_to == 8
    assert live["price"] == 4126.0


def test_minutes_before_entry_are_ignored(setup):
    before = pd.DataFrame({"open": 4130, "high": 4140, "low": 4100, "close": 4130},
                          index=[ENTRY_TIME - pd.Timedelta(minutes=5)])
    setup.bars["1min"] = pd.concat([before, m1([(0, 4118, 4119, 4116, 4117)])])
    track(setup)
    with db.session(setup.path) as conn:
        row = db.all_signals(conn)[0]
    assert row["status"] == "open" and not setup.sent


def test_tp1_then_running(setup):
    setup.bars["1min"] = m1([(1, 4115, 4116, 4107.50, 4108)])  # ask low 4107.80 <= TP1 4107.96
    track(setup)
    with db.session(setup.path) as conn:
        row = db.all_signals(conn)[0]
    assert row["status"] == "tp1" and "TP1 hit" in setup.sent[-1][0]


def test_failed_send_is_retried_not_lost(setup):
    from app.telegram_bot import TelegramError

    setup.bars["1min"] = m1([(1, 4115, 4116, 4107.50, 4108)])
    real = setup.eng.tg.send_message
    setup.eng.tg.send_message = lambda *a, **k: (_ for _ in ()).throw(TelegramError("Bad Gateway"))
    track(setup)
    assert not setup.sent
    with db.session(setup.path) as conn:
        pending = conn.execute("SELECT attempts, status, next_try FROM outbox").fetchone()
        assert pending["status"] == "pending" and pending["attempts"] == 1
        conn.execute("UPDATE outbox SET next_try = '2000-01-01'")  # pretend the retry time has come
    setup.eng.tg.send_message = real
    assert setup.eng.flush_outbox() == 1
    assert "TP1 hit" in setup.sent[-1][0]
    assert setup.eng.flush_outbox() == 0  # sent once, never twice


def test_tracking_interval_follows_the_daily_allowance(setup, monkeypatch):
    noon = pd.Timestamp("2026-10-07 12:00", tz="UTC")  # a Wednesday
    monkeypatch.setattr(engine_mod, "settings", SimpleNamespace(**{**vars(engine_mod.settings),
                                                                   "daily_credit_limit": 100_000}))
    setup.bars["1min"] = m1([(0, 4118, 4119, 4116, 4117)])
    with db.session(setup.path) as conn:
        assert setup.eng.track_minutes(conn, noon) == 1
    for _ in range(3):
        track(setup)
    with db.session(setup.path) as conn:
        assert setup.eng.credits_today(conn) == 3
        assert setup.eng.track_minutes(conn, noon) == 5  # TRACK_CREDIT_BUDGET (3) reached
    # Real free plan: with 790 of 800 used, nothing is left after reserving the remaining 15-minute checks.
    monkeypatch.setattr(engine_mod, "settings", SimpleNamespace(**{**vars(engine_mod.settings),
                                                                   "daily_credit_limit": 800, "track_credit_budget": 700}))
    with db.session(setup.path) as conn:
        db.kv_set(conn, f"credits_{pd.Timestamp.now(tz='UTC'):%Y-%m-%d}", "790")
        assert setup.eng.track_minutes(conn, noon) == 15
        db.kv_set(conn, f"credits_{pd.Timestamp.now(tz='UTC'):%Y-%m-%d}", "400")
        assert 1 < setup.eng.track_minutes(conn, noon) <= 5  # spread the rest over the remaining day


def test_no_tracking_while_market_closed(setup, monkeypatch):
    calls = []
    setup.eng.feed.candles = lambda *a: calls.append(a)
    monkeypatch.setattr(engine_mod.market, "is_open", lambda ts: False)
    assert setup.eng.track_tick() == 5
    assert not calls


def test_live_trade_maths(monkeypatch):
    from app.web import main

    monkeypatch.setattr(main, "settings", SimpleNamespace(**{**vars(main.settings), "pip_size": 0.10,
                                                             "contract_oz": 100, "lot_size": 0.20, "live_spread": 0.30}))
    row = {"direction": "SELL", "entry": 4117.96, "sl": 4127.96, "tp1": 4107.96, "tp2": 4087.96, "strategy": "orb",
           "status": "open"}
    t = main.live_trade(row, {"price": 4120.00, "time": "x"})
    assert t["price"] == 4120.30                      # what closing the SELL costs (ask)
    assert t["pnl_pips"] == -23 and t["pnl_usd"] == -47 and t["pnl_r"] == -0.23
    assert t["to_sl"] == 77 and t["to_tp1"] == 123 and t["to_tp2"] == 323
    assert t["setup"] == "NY open breakout"
    assert 0 < t["position"] < t["entry_pos"] < t["tp1_pos"] < 1


def test_live_trade_after_tp1_counts_the_banked_half(monkeypatch):
    from app.web import main

    monkeypatch.setattr(main, "settings", SimpleNamespace(**{**vars(main.settings), "pip_size": 0.10,
                                                             "contract_oz": 100, "lot_size": 0.20, "live_spread": 0.0}))
    row = {"direction": "BUY", "entry": 2000.0, "sl": 1990.0, "tp1": 2010.0, "tp2": 2030.0, "strategy": "session_breakout",
           "status": "tp1"}
    t = main.live_trade(row, {"price": 2005.0, "time": "x"})
    assert t["pnl_r"] == 0.75                 # half +1R banked, half +0.5R open
    assert t["stop"] == 2000.0 and t["to_sl"] == 50   # stop moved to entry


def test_status_page_shows_live_trade(setup, monkeypatch, sign_in):
    from app.web import auth, main

    monkeypatch.setattr(main, "settings", SimpleNamespace(**{**vars(main.settings), "database_path": setup.path,
                                                             "lot_size": 0.20, "pip_size": 0.10, "contract_oz": 100,
                                                             "live_spread": 0.30}))
    with db.session(setup.path) as conn:
        db.kv_set(conn, "live_price", json.dumps({"price": 4110.0, "time": ENTRY_TIME.isoformat()}))
    with TestClient(main.app) as client:
        sign_in(client)
        r = client.get("/status")
    assert r.status_code == 200
    for text in ("#1 SELL", "NY open breakout", "+77 pips", "To stop loss", "See it on the chart"):
        assert text in r.text, text
