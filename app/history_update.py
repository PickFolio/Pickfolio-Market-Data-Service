from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

import pytz

from .market_history import fetch_daily_history
from .repositories import (
    get_core_universe_symbols_with_latest_history,
    upsert_price_history,
)


logger = logging.getLogger(__name__)


def _latest_trading_date(candles) -> date | None:
    if not candles:
        return None
    return max(candle.trading_date for candle in candles)


def run_incremental_stock_history_update(
    limit: int | None = None,
    history_delay: float = 0.5,
    lookback_days: int = 10,
) -> dict:
    """Refresh recent daily candles for core-universe stocks.

    This job is intentionally safe to rerun. It fetches a short recent window
    and relies on stock_price_history's (symbol, trading_date) upsert.
    """
    started_at = datetime.now(pytz.UTC)
    today_ist = datetime.now(pytz.timezone("Asia/Kolkata")).date()
    symbols = get_core_universe_symbols_with_latest_history()
    if limit is not None:
        symbols = symbols[:limit]

    result = {
        "started_at": started_at.isoformat(),
        "completed_at": None,
        "symbols_seen": len(symbols),
        "symbols_updated": 0,
        "symbols_skipped": 0,
        "candles_upserted": 0,
        "failures": 0,
        "latest_trading_date": None,
    }

    period = "5d" if lookback_days <= 5 else "1mo"
    logger.info(
        "Starting incremental stock history update for %s symbols with period=%s.",
        len(symbols),
        period,
    )

    latest_seen: date | None = None
    for index, (symbol, stored_latest) in enumerate(symbols, start=1):
        try:
            if stored_latest and stored_latest >= today_ist:
                result["symbols_skipped"] += 1
                latest_seen = max(latest_seen, stored_latest) if latest_seen else stored_latest
                continue

            yahoo_symbol, candles = fetch_daily_history(symbol, period=period, interval="1d")
            if not candles:
                result["symbols_skipped"] += 1
                logger.warning("[%s/%s] %s: no recent candles returned.", index, len(symbols), symbol)
                continue

            upserted = upsert_price_history(candles)
            newest = _latest_trading_date(candles)
            if newest:
                latest_seen = max(latest_seen, newest) if latest_seen else newest

            result["symbols_updated"] += 1
            result["candles_upserted"] += upserted
            logger.info(
                "[%s/%s] %s: upserted %s recent candles; stored_latest=%s newest=%s.",
                index,
                len(symbols),
                yahoo_symbol,
                upserted,
                stored_latest,
                newest,
            )
        except Exception as exc:
            result["failures"] += 1
            logger.exception("[%s/%s] Failed incremental history update for %s: %s", index, len(symbols), symbol, exc)
        finally:
            time.sleep(history_delay)

    completed_at = datetime.now(pytz.UTC)
    result["completed_at"] = completed_at.isoformat()
    result["latest_trading_date"] = latest_seen.isoformat() if latest_seen else None
    logger.info("Incremental stock history update complete: %s", result)
    return result


def seconds_until_next_history_run(hour_ist: int, minute_ist: int) -> float:
    india_tz = pytz.timezone("Asia/Kolkata")
    now = datetime.now(india_tz)
    target = now.replace(hour=hour_ist, minute=minute_ist, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 0.0)
