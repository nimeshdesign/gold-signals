"""Download XAUUSD candles from Twelve Data to a CSV (datetime,open,high,low,close in UTC).

  python -m scripts.download --interval 1min --start 2026-04-01 --out data/xauusd_1min.csv

The free plan returns 5000 candles per request and allows 8 requests a minute, so long 1-minute
histories take a few minutes. Each request uses 1 of the 800 daily credits.
"""
import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.data_feed import TwelveDataFeed  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="1min")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    df = TwelveDataFeed(settings.twelve_data_api_key, settings.symbol).history(args.interval, args.start, args.end)
    df.reset_index().to_csv(args.out, index=False)
    print(f"Saved {len(df)} {args.interval} candles ({df.index[0]} -> {df.index[-1]}) to {args.out}", flush=True)


if __name__ == "__main__":
    main()
