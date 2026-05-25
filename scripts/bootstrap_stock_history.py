from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.bootstrap import run_stock_history_bootstrap


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bootstrap_stock_history")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap Pickfolio stock metadata and 1Y price history.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of stocks to process, useful for smoke tests.",
    )
    parser.add_argument(
        "--skip-history",
        action="store_true",
        help="Only upsert stock metadata; do not call yfinance.",
    )
    parser.add_argument(
        "--history-delay",
        type=float,
        default=1.0,
        help="Delay between yfinance history calls in seconds.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    failures = run_stock_history_bootstrap(
        limit=args.limit,
        skip_history=args.skip_history,
        history_delay=args.history_delay,
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
