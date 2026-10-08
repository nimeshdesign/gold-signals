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


def test_sell_stop_hit_by_ask_is_announced_within_a_minute(setup):
    # 13:13 bid high 4127.70 + 0.30 spread = 4128.00 >= SL 4127.96 (the broker's ask touched the stop).
    setup.bars["1min"] = m1([(0, 4118, 4119, 4116, 4117), (13, 4125, 4127.70, 4124, 4126)])
    with db.session(setup.path) as conn:
        setup.eng.track_live(conn)
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
    with db.session(setup.path) as conn:
        setup.eng.track_live(conn)
        row = db.all_signals(conn)[0]
    assert row["status"] == "open" and not setup.sent


def test_tp1_then_running(setup):
    setup.bars["1min"] = m1([(1, 4115, 4116, 4107.50, 4108)])  # ask low 4107.80 <= TP1 4107.96
    with db.session(setup.path) as conn:
        setup.eng.track_live(conn)
        row = db.all_signals(conn)[0]
    assert row["status"] == "tp1" and "TP1 hit" in setup.sent[-1][0]


def test_credit_budget_slows_tracking(setup):
    setup.bars["1min"] = m1([(0, 4118, 4119, 4116, 4117)])
    with db.session(setup.path) as conn:
        assert setup.eng.track_minutes(conn) == 1
        for _ in range(3):
            setup.eng.track_live(conn)
        assert setup.eng.credits_today(conn) == 3
        assert setup.eng.track_minutes(conn) == 5


def test_live_trade_maths(monkeypatch):
    from app.web import main

    monkeypatch.setattr(main, "settings", SimpleNamespace(**{**vars(main.settings), "pip_size": 0.10,
                                                             "contract_oz": 100, "lot_size": 0.20, "live_spread": 0.30}))
    row = {"direction": "SELL", "entry": 4117.96, "sl": 4127.96, "tp1": 4107.96, "tp2": 4087.96, "strategy": "orb"}
    t = main.live_trade(row, {"price": 4120.00, "time": "x"})
    assert t["price"] == 4120.30                      # what closing the SELL costs (ask)
    assert t["pnl_pips"] == -23 and t["pnl_usd"] == -47 and t["pnl_r"] == -0.23
    assert t["to_sl"] == 77 and t["to_tp1"] == 123 and t["to_tp2"] == 323
    assert t["setup"] == "NY open breakout"
    assert 0 < t["position"] < t["entry_pos"] < t["tp1_pos"] < 1


def test_status_page_shows_live_trade(setup, monkeypatch):
    from app.web import auth, main

    monkeypatch.setattr(main, "settings", SimpleNamespace(**{**vars(main.settings), "database_path": setup.path,
                                                             "lot_size": 0.20, "pip_size": 0.10, "contract_oz": 100,
                                                             "live_spread": 0.30}))
    with db.session(setup.path) as conn:
        db.kv_set(conn, "live_price", json.dumps({"price": 4110.0, "time": ENTRY_TIME.isoformat()}))
    with TestClient(main.app) as client:
        client.cookies.set(auth.COOKIE, auth.make_session("admin", main._secret(), 3600))
        r = client.get("/status")
    assert r.status_code == 200
    for text in ("#1 SELL", "NY open breakout", "+77 pips", "To stop loss", "See it on the chart"):
        assert text in r.text, text
