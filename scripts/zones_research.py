"""Test the multi-timeframe zone strategy on 1-minute data: choose on the early months, check on the later ones.

  python -m scripts.zones_research --csv data/xauusd_1min_2026-04-01_now.csv --split 2026-08-01
"""
import argparse
import itertools
import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.data_feed import load_csv  # noqa: E402
from app.simulator import SimConfig, metrics, simulate  # noqa: E402
from app.zones import compute_zone_signals  # noqa: E402

GRID = {
    "trend": ["1h_ema50", "daily_ema20", "none"],
    "min_score": [1, 2, 3],
    "zone_pips": [10, 20],
    "sl_mode": ["zone", "fixed"],
}
TARGETS = [(1.0, 2.0), (1.0, 3.0)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--split", default="2026-08-01")
    ap.add_argument("--spread", type=float, default=0.30)
    ap.add_argument("--out", default=str(ROOT / "data" / "zones_research.csv"))
    args = ap.parse_args()

    m1 = load_csv(args.csv)
    split = pd.Timestamp(args.split, tz="UTC")
    rows, t0 = [], time.time()
    combos = [dict(zip(GRID, v)) for v in itertools.product(*GRID.values())]
    for k, params in enumerate(combos, 1):
        sig = compute_zone_signals(m1, params)
        for tp1, tp2 in TARGETS:
            tr = simulate(sig, m1, SimConfig(tp1, tp2, args.spread), bar=pd.Timedelta(minutes=1))
            if tr.empty:
                continue
            a, b = metrics(tr[tr.entry_time < split]), metrics(tr[tr.entry_time >= split])
            rows.append({"params": json.dumps(params), "tp1": tp1, "tp2": tp2,
                         "sl_pips": round(tr["risk"].median() / 0.1),
                         **{f"train_{x}": y for x, y in a.items()}, **{f"test_{x}": y for x, y in b.items()}})
        print(f"{k}/{len(combos)} done ({time.time() - t0:.0f}s)", flush=True)

    res = pd.DataFrame(rows)
    res.to_csv(args.out, index=False)
    pd.set_option("display.width", 250, "display.max_colwidth", 90)
    cols = ["params", "tp1", "tp2", "sl_pips", "train_trades", "train_win_rate", "train_net_r", "train_pf",
            "test_trades", "test_win_rate", "test_net_r", "test_pf"]
    ok = res[res.train_trades >= 40].sort_values("train_net_r", ascending=False)
    print(f"\n{len(res)} settings. Share profitable: train {(res.train_net_r > 0).mean():.0%}, "
          f"test {(res.test_net_r > 0).mean():.0%}")
    print("\nTop 8 by TRAINING result (Apr-Jul), with their unseen TEST result (Aug-Oct):")
    print(ok[cols].head(8).to_string(index=False))
    pick = ok.iloc[0]
    print(f"\nCHOSEN on training only: {pick['params']} TP {pick['tp1']}/{pick['tp2']}")
    print(f"  train: {pick['train_trades']} trades, {pick['train_win_rate']}% wins, {pick['train_net_r']:+}R, PF {pick['train_pf']}")
    print(f"  TEST:  {pick['test_trades']} trades, {pick['test_win_rate']}% wins, {pick['test_net_r']:+}R, PF {pick['test_pf']}")


if __name__ == "__main__":
    main()
