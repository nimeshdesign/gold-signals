"""Fill a SEPARATE preview database with backtest trades so the website can be checked with data.

  python -m scripts.load_demo                      # reads backtest_trades.csv -> data/demo.db
  set DATABASE_PATH=data/demo.db & set SITE_BANNER=Preview: backtest results, not live signals
  uvicorn app.web.main:app

Never point the live site at this database: these are simulated trades, not signals anyone received.
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.data_feed import load_csv  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(ROOT / "backtest_trades.csv"))
    ap.add_argument("--db", default=str(ROOT / "data" / "demo.db"))
    ap.add_argument("--candles", default=str(ROOT / "data" / "xauusd_15min_2023-10-01_now.csv"),
                    help="15-min price CSV to include for the chart page (skipped if missing)")
    args = ap.parse_args()

    path = Path(args.db)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    db.init_db(path)

    trades = pd.read_csv(args.csv, parse_dates=["entry_time", "closed_at"])
    with db.session(path) as conn:
        for t in trades.itertuples():
            sid = db.insert_signal(
                conn, symbol="XAUUSD", direction=t.direction, entry=t.entry, sl=t.sl, tp1=t.tp1, tp2=t.tp2,
                bar_time=(t.entry_time - pd.Timedelta(minutes=15)).isoformat(),
                created_at=t.entry_time.isoformat(),
                strategy=getattr(t, "strategy", None) or "session_breakout",
                main=(getattr(t, "strategy", None) or "session_breakout") == "session_breakout",
            )
            # Scored like the live tracker: SELL spread included (checked at the ask), BUY spread not.
            db.update_signal(conn, sid, status=t.outcome, result_r=t.result_r_gross, closed_at=t.closed_at.isoformat())
    print(f"Loaded {len(trades)} backtest trades into {path}")
    if Path(args.candles).exists():
        with db.session(path) as conn:
            n = db.upsert_candles(conn, load_csv(args.candles))
        print(f"Loaded {n} candles for the chart page")


if __name__ == "__main__":
    main()
