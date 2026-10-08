"""Walk-forward test of the session breakout, with and without add-on filters.

  python -m scripts.walkforward --csv data/xauusd_15min_2023-10-01_now.csv

Method (as in the Aurum research system):
  * Every 3 months, choose the best setting using ONLY the previous 12 months of trades.
  * Trade the next 3 months with that setting. Only these out-of-sample months are scored.
  * Repeat until the data runs out, then join the out-of-sample trades.
Spread is charged on every trade. Each add-on is its own run: its setting pool is the base grid plus
the add-on's values, so it only "wins" if choosing it on past data actually helps on future data.
"""
import argparse
import itertools
import json
import math
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.data_feed import load_csv  # noqa: E402
from app.simulator import SimConfig, metrics, simulate  # noqa: E402
from app.strategies import build_data, compute_signals  # noqa: E402

BASE_GRID = {"range_start": [0], "range_end": [6, 7], "window_end": [12, 16], "trend_tf": ["1day"],
             "trend_len": [0, 20], "range_frac": [0.5, 1.0], "atr_cap": [2, 3]}
TARGETS = [(0.8, 2.0), (1.0, 2.0), (1.0, 3.0), (1.5, 3.0)]
ADDONS = {
    "base": {},
    "adx": {"adx_min": [None, 20, 25]},
    "overlap": {"window_start": [None, 12]},
    "pdh_pdl": {"pd_filter": [None, "beyond", "room"]},
}


def grid(axes: dict) -> list[dict]:
    keys = list(axes)
    return [dict(zip(keys, combo)) for combo in itertools.product(*axes.values())]


def key(params: dict, tp: tuple) -> str:
    return json.dumps({k: v for k, v in sorted(params.items()) if v is not None} | {"tp": tp}, sort_keys=True)


def walk_forward(pool: list[str], trades: dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp,
                 train_months: int, test_months: int, min_trades: int):
    oos, picks = [], []
    test_start = start + pd.DateOffset(months=train_months)
    while test_start < end:
        test_end = min(test_start + pd.DateOffset(months=test_months), end)
        train_start = test_start - pd.DateOffset(months=train_months)
        best, best_r = None, -math.inf
        for k in pool:
            t = trades[k]
            tr = t[(t["entry_time"] >= train_start) & (t["entry_time"] < test_start)]
            if len(tr) >= min_trades and tr["result_r"].sum() > best_r:
                best, best_r = k, tr["result_r"].sum()
        if best is not None:
            t = trades[best]
            te = t[(t["entry_time"] >= test_start) & (t["entry_time"] < test_end)]
            oos.append(te)
            picks.append({"test": f"{test_start:%Y-%m}..{(test_end - pd.Timedelta(days=1)):%Y-%m}",
                          "train_r": round(best_r, 1), "test_trades": len(te),
                          "test_r": round(te["result_r"].sum(), 1), "setting": best})
        test_start = test_end
    return (pd.concat(oos) if oos else pd.DataFrame(columns=["entry_time", "result_r"])), picks


def t_stat(r: pd.Series) -> float:
    return float(r.mean() / r.std(ddof=1) * math.sqrt(len(r))) if len(r) > 2 and r.std() > 0 else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--spread", type=float, default=0.30)
    ap.add_argument("--train-months", type=int, default=12)
    ap.add_argument("--test-months", type=int, default=3)
    ap.add_argument("--min-trades", type=int, default=40)
    ap.add_argument("--out", default=str(ROOT / "data" / "walkforward_results.json"))
    args = ap.parse_args()

    m15 = load_csv(args.csv)
    data = build_data(m15)
    start, end = m15.index[0].normalize(), m15.index[-1]

    trades: dict[str, pd.DataFrame] = {}
    pools: dict[str, list[str]] = {}
    t0 = time.time()
    for name, extra in ADDONS.items():
        pools[name] = []
        for params in grid({**BASE_GRID, **extra}):
            signals = None
            for tp in TARGETS:
                k = key(params, tp)
                pools[name].append(k)
                if k in trades:
                    continue
                if signals is None:
                    signals = compute_signals("session_breakout", data, params)
                t = simulate(signals, m15, SimConfig(tp1_r=tp[0], tp2_r=tp[1], spread=args.spread))
                trades[k] = t[["entry_time", "direction", "outcome", "result_r"]] if len(t) else \
                    pd.DataFrame(columns=["entry_time", "direction", "outcome", "result_r"])
        print(f"{name}: {len(pools[name])} settings ({len(trades)} simulated, {time.time() - t0:.0f}s)", flush=True)

    results = {}
    print(f"\nWalk-forward: choose on {args.train_months} months, trade the next {args.test_months}, "
          f"spread ${args.spread}\n")
    for name, pool in pools.items():
        oos, picks = walk_forward(pool, trades, start, end, args.train_months, args.test_months, args.min_trades)
        m = metrics(oos)
        r = oos["result_r"] if len(oos) else pd.Series(dtype=float)
        yearly = oos.groupby(oos["entry_time"].dt.year)["result_r"].sum().round(1).to_dict() if len(oos) else {}
        used_addon = sum(1 for p in picks if any(f in p["setting"] for f in ("adx_min", "window_start", "pd_filter")))
        results[name] = {"metrics": m, "t_stat": round(t_stat(r), 2), "windows": len(picks),
                         "windows_profitable": sum(1 for p in picks if p["test_r"] > 0),
                         "addon_chosen_in": used_addon, "yearly": yearly, "picks": picks}
        print(f"{name:8s} trades {m['trades']:4d} | win {m['win_rate']:5.1f}% | net {m['net_r']:+6.1f}R | "
              f"avg {m['avg_r']:+.3f}R | PF {m['pf']:.2f} | maxDD {m['max_dd']:5.1f}R | t {t_stat(r):5.2f} | "
              f"profitable windows {results[name]['windows_profitable']}/{len(picks)} | add-on chosen {used_addon}/{len(picks)}")

    Path(args.out).write_text(json.dumps(results, indent=1, default=str))
    print(f"\nDetails (chosen setting per window): {args.out}")


if __name__ == "__main__":
    main()
