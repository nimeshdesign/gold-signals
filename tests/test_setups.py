import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from app import db
from app import engine as engine_mod
from app.engine import Engine
from app.telegram_bot import format_day_end, format_signal

ORB = {"name": "orb", "label": "NY open breakout",
       "params": {"open_hm": "08:15", "range_min": 30, "window_end": 12, "sl_fixed": 10.0}}


class QuietNews:
    def blocking_event(self, now=None):
        return None

    def events_between(self, start, end):
        return []


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "s.db"
    monkeypatch.setattr(db, "settings", SimpleNamespace(database_path=path))
    db.init_db(path)
    return path


def make_engine(monkeypatch, extras=(ORB,), **overrides):
    monkeypatch.setattr(engine_mod, "settings", SimpleNamespace(**{
        **vars(engine_mod.settings), "extra_strategies": list(extras), "max_open_signals": 2,
        "news_filter_enabled": False, **overrides}))
    sent = []

    class TG:
        dry_run = False

        def send_message(self, chat, text, reply_to=None):
            sent.append(text)
            return len(sent)

    return Engine(feed=object(), telegram=TG(), news=QuietNews()), sent


def test_two_setups_can_signal_on_the_same_candle(tmp_db):
    with db.session(tmp_db) as conn:
        common = dict(symbol="XAUUSD", direction="BUY", entry=1, sl=0, tp1=2, tp2=3,
                      bar_time="2026-10-08T13:00:00+00:00", created_at="2026-10-08T13:15:00+00:00")
        a = db.insert_signal(conn, **common)
        b = db.insert_signal(conn, **common, strategy="orb", main=False)
        again = db.insert_signal(conn, **common, strategy="orb", main=False)
        rows = db.all_signals(conn)
    assert a and b and again is None
    assert sorted(r["strategy"] for r in rows) == ["orb", "session_breakout"]


def test_old_database_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, entry REAL, sl REAL, "
                "tp1 REAL, tp2 REAL, bar_time TEXT UNIQUE, created_at TEXT, status TEXT DEFAULT 'open', result_r REAL, "
                "tp1_hit_at TEXT, closed_at TEXT, last_checked TEXT, telegram_msg_id INTEGER, atr REAL, rsi REAL)")
    con.execute("INSERT INTO signals (symbol, direction, entry, sl, tp1, tp2, bar_time, created_at) "
                "VALUES ('XAUUSD','SELL',1,2,0,-1,'t','t')")
    con.commit()
    con.close()
    db.init_db(path)
    with db.session(path) as conn:
        assert db.all_signals(conn)[0]["strategy"] == "session_breakout"


def test_engine_lists_setups(monkeypatch):
    eng, _ = make_engine(monkeypatch)
    assert [s["label"] for s in eng.setups] == ["Asian breakout", "NY open breakout"]
    assert eng.setups[1]["params"]["sl_fixed"] == 10.0 and eng.setups[1]["params"]["trend_len"] == 20  # defaults kept


@pytest.mark.parametrize("day,expected_utc", [("2026-10-08", "12:15"), ("2026-12-08", "13:15")])
def test_ny_session_follows_daylight_saving(monkeypatch, day, expected_utc):
    eng, _ = make_engine(monkeypatch)
    ny = eng.ny_session(pd.Timestamp(f"{day} 10:00", tz="UTC"))
    assert pd.Timestamp(ny["range_start"]).strftime("%H:%M") == expected_utc
    assert pd.Timestamp(ny["window_end"]) - pd.Timestamp(ny["range_start"]) == pd.Timedelta(hours=3, minutes=45)


def test_day_end_waits_for_ny_window(tmp_db, monkeypatch):
    eng, sent = make_engine(monkeypatch)
    winter_day = pd.Timestamp("2026-12-08", tz="UTC")  # NY window ends 17:00 UTC
    snap = {"price": 4000, "trend": {"direction": "DOWN", "close": 1, "ema": 2},
            "range": {"start": 0, "end": 6, "window_end": 16, "high": 4010, "low": 3990},
            "window_moves": None, "skip_note": None, "ny_setup": eng.ny_session(winter_day + pd.Timedelta(hours=7))}
    with db.session(tmp_db) as conn:
        eng.send_daily_updates(conn, snap, winter_day + pd.Timedelta(hours=7))
        assert "Second setup" in sent[-1] and "NY open breakout" in sent[-1]
        eng.send_daily_updates(conn, snap, winter_day + pd.Timedelta(hours=16, minutes=30))
        assert len(sent) == 1  # NY window still open
        eng.send_daily_updates(conn, snap, winter_day + pd.Timedelta(hours=17, minutes=1))
        assert len(sent) == 2 and "either session" in sent[-1]


def test_messages_name_the_setup():
    text = format_signal("XAUUSD", "BUY", 4000, 3990, 4010, 4030, 1.0, 3.0, setup="NY open breakout")
    assert text.splitlines()[0].endswith("· NY open breakout")
    rows = [{"strategy": "session_breakout", "direction": "SELL", "entry": 4100.0, "status": "sl", "result_r": -1.0},
            {"strategy": "orb", "direction": "SELL", "entry": 4090.0, "status": "tp1", "result_r": None}]

    class Row(dict):
        def keys(self):
            return super().keys()

    text = format_day_end("XAUUSD", {"range": {"end": 6}}, [Row(r) for r in rows])
    assert "Today's signals (2)" in text and "Asian breakout" in text and "NY open breakout" in text
