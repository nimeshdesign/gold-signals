"""Backtest the configured strategy (STRATEGY / STRATEGY_PARAMS / TP1_R / TP2_R in .env) on XAUUSD history.

Uses the same signal code (`app.strategies`) and trade management (`app.outcome`) as the live engine,
with the spread charged realistically (see `app.simulator`).

Data, either:
  --download --start 2023-10-01        pull 15-min candles from Twelve Data (cached to data/)
  --csv path/to/m15.csv                your own file: datetime,open,high,low,close (UTC open time)

The news filter is NOT applied (no free historical calendar), so live trading will skip a few trades.

Examples:
  python -m scripts.backtest --download --start 2023-10-01
  python -m scripts.backtest --csv data/xauusd_15min_2023-10-01_now.csv --spread 0.5
  python -m scripts.backtest --csv ... --strategy ema_cross --params "{\"tf\": \"1h\"}"
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.data_feed import TwelveDataFeed, load_csv  # noqa: E402
from app.simulator import SimConfig, metrics, simulate  # noqa: E402
from app.strategies import DEFAULT_PARAMS, build_data, compute_signals  # noqa: E402


def run(m15: pd.DataFrame, strategy: str, params: dict, tp1_r: float, tp2_r: float, spread: float,
        extras: list[dict] | None = None) -> pd.DataFrame:
    """Simulate the main strategy plus any extra setups together, sharing the max-open-trades limit."""
    data = build_data(m15)
    signals = compute_signals(strategy, data, params).assign(strategy=strategy)
    for extra in extras or []:
        extra_params = {**DEFAULT_PARAMS[extra["name"]], **extra.get("params", {})}
        signals = pd.concat([signals, compute_signals(extra["name"], data, extra_params).assign(strategy=extra.get("id", extra["name"]))])
    signals = signals[signals["signal"] != 0].sort_index(kind="stable")  # main setup first on ties
    cfg = SimConfig(tp1_r=tp1_r, tp2_r=tp2_r, spread=spread, max_open=settings.max_open_signals,
                    expiry_hours=settings.signal_expiry_hours)
    return simulate(signals, m15, cfg)


def summarize(trades: pd.DataFrame) -> str:
    if trades.empty:
        return "No trades."
    m = metrics(trades)
    r = trades["result_r"]
    by_year = trades.groupby(trades["entry_time"].dt.year)["result_r"].agg(
        trades="count", win_rate=lambda s: round((s > 0).mean() * 100, 1), net_r="sum").round(2)
    by_side = trades.groupby("direction")["result_r"].agg(
        trades="count", win_rate=lambda s: round((s > 0).mean() * 100, 1), net_r="sum").round(2)
    return "\n".join([
        f"Period:        {trades['entry_time'].min():%Y-%m-%d} -> {trades['closed_at'].max():%Y-%m-%d}",
        f"Trades:        {m['trades']}",
        f"Win rate:      {m['win_rate']}%",
        f"Total:         {m['net_r']:+}R after spread   ({trades['result_r_gross'].sum():+.1f}R as the live tracker scores it)",
        f"Avg / trade:   {m['avg_r']:+}R",
        f"Profit factor: {m['pf']}",
        f"Max drawdown:  {m['max_dd']}R",
        f"Longest losing streak: {_longest_streak(r <= 0)}",
        f"Median stop distance: ${trades['risk'].median():.2f}",
        "", "Outcomes:", trades["outcome"].value_counts().to_string(),
        "", "By year:", by_year.to_string(),
        "", "By side:", by_side.to_string(),
        *(["", "By setup:", trades.groupby("strategy")["result_r"].agg(
            trades="count", win_rate=lambda s: round((s > 0).mean() * 100, 1), net_r="sum").round(2).to_string()]
          if trades["strategy"].nunique() > 1 else []),
    ])


def _longest_streak(mask: pd.Series) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="15-min OHLC CSV")
    src.add_argument("--download", action="store_true", help="download from Twelve Data")
    ap.add_argument("--start", default="2023-10-01")
    ap.add_argument("--end")
    ap.add_argument("--strategy", default=settings.strategy_name)
    ap.add_argument("--params", help="JSON overrides for the strategy settings")
    ap.add_argument("--tp1", type=float, default=settings.strategy.tp1_r)
    ap.add_argument("--tp2", type=float, default=settings.strategy.tp2_r)
    ap.add_argument("--spread", type=float, default=0.30, help="spread in $ per ounce (default 0.30)")
    ap.add_argument("--out", default="backtest_trades.csv")
    ap.add_argument("--no-extras", action="store_true", help="test only the main strategy")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.download:
        cache = ROOT / "data" / f"xauusd_15min_{args.start}_{args.end or 'now'}.csv"
        if cache.exists():
            m15 = load_csv(str(cache))
        else:
            feed = TwelveDataFeed(settings.twelve_data_api_key, settings.symbol)
            m15 = feed.history("15min", args.start, args.end)
            m15.reset_index().to_csv(cache, index=False)
            print(f"Saved {len(m15)} candles to {cache}")
    else:
        m15 = load_csv(args.csv)

    params = {**DEFAULT_PARAMS[args.strategy]}
    if args.strategy == settings.strategy_name:
        params.update(settings.strategy_params)
    if args.params:
        params.update(json.loads(args.params))
    print(f"Strategy: {args.strategy} {params}  TP1={args.tp1}R TP2={args.tp2}R  spread=${args.spread}\n")

    extras = settings.extra_strategies if args.strategy == settings.strategy_name and not args.no_extras else []
    if extras:
        print("Extra setups: " + ", ".join(e["name"] for e in extras) + f" (max {settings.max_open_signals} open)\n")
    trades = run(m15, args.strategy, params, args.tp1, args.tp2, args.spread, extras)
    print(summarize(trades))
    if not trades.empty:
        trades.to_csv(args.out, index=False)
        print(f"\nTrade list written to {args.out}")


if __name__ == "__main__":
    main()
