from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from .config import SCREENER_REQUEST_DELAY_SEC
from .db import run_migrations
from .market_history import fetch_daily_history
from .repositories import (
    StockMetadata,
    count_price_history_rows,
    get_bootstrap_state,
    mark_history_initialized,
    set_bootstrap_state,
    upsert_price_history,
    upsert_stock_master,
)
from .screener import ScreenerCompany, fetch_company_detail, fetch_core_universe


logger = logging.getLogger(__name__)
STOCK_HISTORY_BOOTSTRAP_COMPLETE_KEY = "stock_history_bootstrap_complete"


def _parse_completion_marker(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        marker = json.loads(value)
    except json.JSONDecodeError:
        logger.warning("Ignoring legacy or invalid stock bootstrap marker: %s", value)
        return None
    if not isinstance(marker, dict):
        logger.warning("Ignoring legacy or invalid stock bootstrap marker: %s", value)
        return None
    if not marker.get("completed"):
        return None
    if int(marker.get("candles", 0)) <= 0:
        return None
    return marker


def upsert_core_metadata(
    company: ScreenerCompany,
    yahoo_symbol: str | None = None,
    detail_metadata: dict | None = None,
) -> str:
    symbol = yahoo_symbol or f"{company.symbol}.NS"
    detail_metadata = detail_metadata or {}
    upsert_stock_master(
        StockMetadata(
            symbol=symbol,
            company_name=detail_metadata.get("company_name") or company.company_name,
            exchange="NSE" if symbol.endswith(".NS") else "BSE" if symbol.endswith(".BO") else None,
            yahoo_symbol=symbol,
            screener_url=detail_metadata.get("detail_url") or company.screener_url,
            market_cap=company.market_cap,
            sector=detail_metadata.get("sector") or company.sector,
            industry=detail_metadata.get("industry") or company.industry,
            is_in_core_universe=True,
            is_active=True,
            raw_metadata={
                "source": "screener",
                "screener_symbol": company.symbol,
                **company.raw_metadata,
                "detail": detail_metadata,
            },
        )
    )
    return symbol


def process_company(index: int, total: int, company: ScreenerCompany, skip_history: bool, history_delay: float) -> bool:
    detail_metadata = fetch_company_detail(company.symbol)

    if skip_history:
        symbol = upsert_core_metadata(company, detail_metadata=detail_metadata)
        logger.info("[%s/%s] Metadata upserted for %s; history skipped", index, total, symbol)
        return True

    logger.info("[%s/%s] Fetching %s...", index, total, company.symbol)
    try:
        yahoo_symbol, candles = fetch_daily_history(company.symbol)
        upsert_core_metadata(company, yahoo_symbol=yahoo_symbol, detail_metadata=detail_metadata)

        inserted = upsert_price_history(candles)
        if inserted:
            mark_history_initialized(yahoo_symbol)
        logger.info("[%s/%s] %s: upserted %s candles", index, total, yahoo_symbol, inserted)
        return True
    except Exception as exc:
        fallback_symbol = upsert_core_metadata(company, detail_metadata=detail_metadata)
        logger.exception("[%s/%s] Failed to fetch/store history for %s: %s", index, total, fallback_symbol, exc)
        return False
    finally:
        time.sleep(history_delay)


def run_stock_history_bootstrap(
    limit: int | None = None,
    skip_history: bool = False,
    history_delay: float = 1.0,
) -> int:
    logger.info("Running market-data migrations...")
    run_migrations()

    logger.info("Fetching core universe from Screener; page delay is %.2fs", SCREENER_REQUEST_DELAY_SEC)
    companies = fetch_core_universe()
    if limit is not None:
        companies = companies[:limit]

    total = len(companies)
    logger.info("Core universe contains %s companies to process", total)

    failures = 0
    for index, company in enumerate(companies, start=1):
        try:
            if not process_company(index, total, company, skip_history, history_delay):
                failures += 1
        except Exception as exc:
            failures += 1
            logger.exception("[%s/%s] Unexpected failure for %s: %s", index, total, company.symbol, exc)

    logger.info("Bootstrap complete. Processed=%s failures=%s", total, failures)
    candle_count = count_price_history_rows()
    if failures == 0 and total > 0 and candle_count > 0 and not skip_history and limit is None:
        set_bootstrap_state(
            STOCK_HISTORY_BOOTSTRAP_COMPLETE_KEY,
            json.dumps(
                {
                    "completed": True,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "companies": total,
                    "candles": candle_count,
                }
            ),
        )
    elif failures == 0 and not skip_history and limit is None:
        logger.warning(
            "Bootstrap did not write completion marker because processed=%s and candles=%s.",
            total,
            candle_count,
        )
    return failures


def run_stock_history_bootstrap_if_needed(limit: int | None = None, history_delay: float = 1.0) -> None:
    try:
        existing_rows = count_price_history_rows()
        marker = _parse_completion_marker(get_bootstrap_state(STOCK_HISTORY_BOOTSTRAP_COMPLETE_KEY))
        if marker:
            expected_candles = int(marker["candles"])
            if existing_rows >= expected_candles:
                logger.info(
                    "Stock history bootstrap skipped; completed marker expects %s candles and table has %s rows.",
                    expected_candles,
                    existing_rows,
                )
                return
            logger.warning(
                "Stock history bootstrap marker expects %s candles but table has %s; rerunning bootstrap.",
                expected_candles,
                existing_rows,
            )

        logger.info(
            "Stock history bootstrap completion marker is missing; starting automatic bootstrap. Existing candles=%s",
            existing_rows,
        )
        run_stock_history_bootstrap(limit=limit, skip_history=False, history_delay=history_delay)
    except Exception:
        logger.exception("Automatic stock history bootstrap failed.")
