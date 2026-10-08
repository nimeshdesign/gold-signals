"""Search strategy settings on TRAINING data, then check the chosen one once on unseen TEST data.

  python -m scripts.research --csv data/xauusd_15min_2023-10-01_now.csv

Rules that keep this honest:
  * Settings are ranked using training trades only (before --split).
  * The winner is chosen before its test result is printed; the test set is never used to choose.
  * Costs (spread) are included in every number.
Many settings are tried, so even the training winner's numbers are optimistic. The test result is
the one to believe, and it still needs paper trading to confirm.
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
from app.strategies import build_data, compute_signals  # noqa: E402


def grid(**axes):
    keys = list(axes)
    for combo in itertools.product(*axes.values()):
        yield dict(zip(keys, combo))


SEARCH = {
    "ema_cross": list(grid(tf=["15min", "1h"], trend_tf=["4h", "1day"], trend_len=[50, 200],
                           fast=[9, 20], slow=[21, 50], atr_mult=[1.5, 2.5])),
    "trend_pullback": list(grid(tf=["15min", "1h"], trend_tf=["4h", "1day"], trend_len=[50],
                                fast=[20], slow=[50, 100], lookback=[3, 6], atr_mult=[1.5, 2.5])),
    "rsi2_reversion": list(grid(tf=["1h", "4h"], trend_tf=["1day"], trend_len=[50, 200],
                                slow=[50, 200], rsi_lo=[5, 10, 15], atr_mult=[2.0, 3.0])),
    "session_breakout": list(grid(range_start=[0], range_end=[6, 7], window_end=[12, 16],
                                  trend_tf=["1day"], trend_len=[0, 20], range_frac=[0.5, 1.0], atr_cap=[2, 3])),
    "orb": list(grid(open_hm=["08:15", "09:30"], range_min=[15, 30, 60], window_end=[11, 13, 15],
                     trend_tf=["1day"], trend_len=[0, 20], range_frac=[0.5, 1.0], atr_cap=[2, 3])),
}
# ema_cross needs fast < slow
SEARCH["ema_cross"] = [p for p in SEARCH["ema_cross"] if p["fast"] < p["slow"]]
TARGETS = [(0.8, 2.0), (1.0, 2.0), (1.0, 3.0), (1.5, 3.0)]


def passes(m: dict, min_trades: int) -> bool:
    return m["trades"] >= min_trades and 50 <= m["win_rate"] <= 60 and m["pf"] >= 1.15 and m["net_r"] > 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--split", default="2026-01-01", help="test period starts here")
    ap.add_argument("--spread", type=float, default=0.30)
    ap.add_argument("--min-trades", type=int, default=80)
    ap.add_argument("--out", default=str(ROOT / "data" / "research_results.csv"))
    ap.add_argument("--only", nargs="+", choices=list(SEARCH), help="research only these strategies")
    args = ap.parse_args()

    m15 = load_csv(args.csv)
    data = build_data(m15)
    split = pd.Timestamp(args.split, tz="UTC")
    rows, t0 = [], time.time()

    for name, param_sets in SEARCH.items():
        if args.only and name not in args.only:
            continue
        for params in param_sets:
            signals = compute_signals(name, data, params)
            for tp1, tp2 in TARGETS:
                trades = simulate(signals, m15, SimConfig(tp1_r=tp1, tp2_r=tp2, spread=args.spread))
                if trades.empty:
                    continue
                train = metrics(trades[trades["entry_time"] < split])
                test = metrics(trades[trades["entry_time"] >= split])
                rows.append({"strategy": name, "params": json.dumps(params), "tp1": tp1, "tp2": tp2,
                             **{f"train_{k}": v for k, v in train.items()},
                             **{f"test_{k}": v for k, v in test.items()}})
        print(f"{name}: {len(param_sets) * len(TARGETS)} settings tested ({time.time() - t0:.0f}s)")

    res = pd.DataFrame(rows)
    res.to_csv(args.out, index=False)
    train_cols = [c for c in res.columns if c.startswith("train_")]
    ok = res[res.apply(lambda r: passes({k[6:]: r[k] for k in train_cols}, args.min_trades), axis=1)]
    print(f"\n{len(res)} settings tested; {len(ok)} pass on TRAINING data "
          f"(win 50-60%, profit factor >= 1.15, >= {args.min_trades} trades, after spread).")

    pd.set_option("display.width", 200, "display.max_colwidth", 120)
    show = ["strategy", "tp1", "tp2", "train_trades", "train_win_rate", "train_net_r", "train_pf", "train_max_dd"]
    if ok.empty:
        print("\nNothing passed. Best training results by net R:")
        print(res.sort_values("train_net_r", ascending=False)[show + ["params"]].head(10).to_string(index=False))
        return

    print("\nBest per strategy on TRAINING data:")
    best_each = ok.sort_values("train_net_r", ascending=False).groupby("strategy").head(1)
    print(best_each[show].to_string(index=False))

    pick = ok.sort_values("train_net_r", ascending=False).iloc[0]
    print("\n=== CHOSEN (by training data only) ===")
    print(f"{pick['strategy']}  TP1={pick['tp1']}R TP2={pick['tp2']}R  params={pick['params']}")
    print(f"TRAIN: {pick['train_trades']} trades, win {pick['train_win_rate']}%, net {pick['train_net_r']}R, "
          f"PF {pick['train_pf']}, max DD {pick['train_max_dd']}R")
    print(f"TEST (unseen {args.split}+): {pick['test_trades']} trades, win {pick['test_win_rate']}%, "
          f"net {pick['test_net_r']}R, PF {pick['test_pf']}, max DD {pick['test_max_dd']}R")
    print(f"\nAll results: {args.out}")


if __name__ == "__main__":
    main()
