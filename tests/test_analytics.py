from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db
from app.web import analytics


def _rows(tmp_path):
    path = tmp_path / "a.db"
    db.init_db(path)
    data = [  # (created, closed, side, status, result)
        ("2026-01-05T07:00:00+00:00", "2026-01-05T09:00:00+00:00", "BUY", "sl", -1.0),
        ("2026-01-06T07:00:00+00:00", "2026-01-06T12:00:00+00:00", "BUY", "tp2", 2.0),
        ("2026-02-02T08:00:00+00:00", "2026-02-02T10:00:00+00:00", "SELL", "be", 0.5),
        ("2026-02-03T08:00:00+00:00", "2026-02-05T08:00:00+00:00", "SELL", "expired", -0.4),
        ("2026-02-04T09:00:00+00:00", None, "SELL", "open", None),
    ]
    with db.session(path) as conn:
        for created, closed, side, status, res in data:
            sid = db.insert_signal(conn, symbol="XAUUSD", direction=side, entry=100, sl=90, tp1=110, tp2=130,
                                   bar_time=created, created_at=created)
            db.update_signal(conn, sid, status=status, result_r=res, closed_at=closed)
        return path, db.all_signals(conn)


def test_compute(tmp_path):
    _, rows = _rows(tmp_path)
    a = analytics.compute(analytics.to_frame(rows))
    assert a.trades == 4 and a.open_count == 1
    assert a.wins == 2 and a.win_rate == 50.0
    assert a.stops == 1 and a.sl_rate == 25.0
    assert a.net_r == pytest.approx(1.1)
    assert a.profit_factor == pytest.approx(2.5 / 1.4, abs=0.01)
    assert a.months_total == 2 and a.months_profitable == 2  # Jan +1.0R, Feb +0.1R
    labels = {o["key"]: o["count"] for o in a.outcomes}
    assert labels == {"sl": 1, "be": 1, "tp2": 1, "expired_loss": 1}
    assert {g.label for g in a.by_side} == {"BUY", "SELL"}
    assert a.avg_hours_to_sl == 2.0
    assert a.longest_losing == 1


def test_filters(tmp_path):
    _, rows = _rows(tmp_path)
    df = analytics.to_frame(rows)
    assert analytics.compute(analytics.filter_frame(df, "all", "BUY")).trades == 2
    # 30 days back from the newest signal (4 Feb) reaches 5 Jan: drops only the 5 Jan stop loss.
    assert analytics.compute(analytics.filter_frame(df, "30d", "all")).trades == 3


def test_page_live_empty_and_backtest(tmp_path, monkeypatch):
    path, _ = _rows(tmp_path)
    empty = tmp_path / "live.db"
    db.init_db(empty)
    from app.web import main

    monkeypatch.setattr(main, "settings", SimpleNamespace(**{**vars(main.settings), "database_path": empty,
                                                             "backtest_database_path": path}))
    monkeypatch.setattr(db, "settings", SimpleNamespace(database_path=empty))
    from app.web import auth

    with TestClient(main.app) as client:
        client.cookies.set(auth.COOKIE, auth.make_session("admin", main._secret(), 3600))
        r = client.get("/analytics")
        assert r.status_code == 200 and "No closed live signals yet" in r.text
        r = client.get("/analytics?src=backtest")
        assert r.status_code == 200
        for text in ("50.0%", "25.0%", "Stop loss hit (full loss)", "Backtest: simulated"):
            assert text in r.text, text
        r = client.get("/analytics?src=backtest&show=sl")
        assert r.text.count("small-chip") == 1  # one row in the trade table
