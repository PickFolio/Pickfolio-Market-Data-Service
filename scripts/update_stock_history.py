from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.db import run_migrations
from app.history_update import run_incremental_stock_history_update


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Incrementally refresh recent Pickfolio stock daily candles.")
    parser.add_argument("--limit", type=int, default=None, help="Optional max number of symbols to process.")
    parser.add_argument(
        "--history-delay",
        type=float,
        default=0.5,
        help="Delay between yfinance history calls in seconds.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=10,
        help="Recent yfinance period window to fetch, in days.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_migrations()
    result = run_incremental_stock_history_update(
        limit=args.limit,
        history_delay=args.history_delay,
        lookback_days=args.lookback_days,
    )
    return 0 if result["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
