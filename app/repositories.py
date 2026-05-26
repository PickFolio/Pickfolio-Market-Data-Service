from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from .db import get_connection


@dataclass(frozen=True)
class StockMetadata:
    symbol: str
    company_name: str | None = None
    exchange: str | None = None
    yahoo_symbol: str | None = None
    screener_url: str | None = None
    market_cap: Decimal | None = None
    sector: str | None = None
    industry: str | None = None
    is_in_core_universe: bool = False
    is_active: bool = True
    history_initialized: bool = False
    raw_metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class PriceCandle:
    symbol: str
    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class NewsHeadline:
    symbol: str
    headline: str
    fetch_time: datetime
    source: str = "yfinance"
    published_at: datetime | None = None
    url: str | None = None
    sentiment_score: float | None = None
    sentiment_method: str | None = None
    event_type: str | None = None
    volume: int = 0
    p_change: float = 0.0
    raw_payload: dict[str, Any] | None = None


def upsert_stock_master(stock: StockMetadata) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO stock_master (
                    symbol,
                    company_name,
                    exchange,
                    yahoo_symbol,
                    screener_url,
                    market_cap,
                    sector,
                    industry,
                    is_in_core_universe,
                    is_active,
                    history_initialized,
                    raw_metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol) DO UPDATE SET
                    company_name = COALESCE(EXCLUDED.company_name, stock_master.company_name),
                    exchange = COALESCE(EXCLUDED.exchange, stock_master.exchange),
                    yahoo_symbol = COALESCE(EXCLUDED.yahoo_symbol, stock_master.yahoo_symbol),
                    screener_url = COALESCE(EXCLUDED.screener_url, stock_master.screener_url),
                    market_cap = COALESCE(EXCLUDED.market_cap, stock_master.market_cap),
                    sector = COALESCE(EXCLUDED.sector, stock_master.sector),
                    industry = COALESCE(EXCLUDED.industry, stock_master.industry),
                    is_in_core_universe = EXCLUDED.is_in_core_universe,
                    is_active = EXCLUDED.is_active,
                    history_initialized = stock_master.history_initialized OR EXCLUDED.history_initialized,
                    raw_metadata = stock_master.raw_metadata || EXCLUDED.raw_metadata
                """,
                (
                    stock.symbol,
                    stock.company_name,
                    stock.exchange,
                    stock.yahoo_symbol,
                    stock.screener_url,
                    stock.market_cap,
                    stock.sector,
                    stock.industry,
                    stock.is_in_core_universe,
                    stock.is_active,
                    stock.history_initialized,
                    Jsonb(stock.raw_metadata or {}),
                ),
            )
        conn.commit()


def upsert_price_history(candles: Iterable[PriceCandle]) -> int:
    rows = list(candles)
    if not rows:
        return 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO stock_price_history (
                    symbol,
                    trading_date,
                    open,
                    high,
                    low,
                    close,
                    volume
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, trading_date) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
                """,
                [
                    (
                        candle.symbol,
                        candle.trading_date,
                        candle.open,
                        candle.high,
                        candle.low,
                        candle.close,
                        candle.volume,
                    )
                    for candle in rows
                ],
            )
        conn.commit()

    return len(rows)


def mark_history_initialized(symbol: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE stock_master
                SET history_initialized = TRUE
                WHERE symbol = %s
                """,
                (symbol,),
            )
        conn.commit()


def get_tracked_symbols() -> list[str]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT symbol
                FROM stock_price_history
                ORDER BY symbol
                """
            )
            return [row[0] for row in cur.fetchall()]


def get_core_universe_symbols_with_latest_history() -> list[tuple[str, date | None]]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(sm.yahoo_symbol, sm.symbol),
                    MAX(sph.trading_date)
                FROM stock_master sm
                LEFT JOIN stock_price_history sph
                    ON sph.symbol = COALESCE(sm.yahoo_symbol, sm.symbol)
                WHERE sm.is_in_core_universe = TRUE
                  AND sm.is_active = TRUE
                GROUP BY COALESCE(sm.yahoo_symbol, sm.symbol)
                ORDER BY COALESCE(sm.yahoo_symbol, sm.symbol)
                """
            )
            return [(row[0], row[1]) for row in cur.fetchall()]


def get_news_universe_symbols(limit: int | None = None) -> list[str]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            query = """
                SELECT DISTINCT COALESCE(NULLIF(yahoo_symbol, ''), symbol)
                FROM stock_master
                WHERE is_active = TRUE
                ORDER BY COALESCE(NULLIF(yahoo_symbol, ''), symbol)
            """
            params: tuple[Any, ...] = ()
            if limit is not None:
                query += " LIMIT %s"
                params = (limit,)
            cur.execute(query, params)
            return [row[0] for row in cur.fetchall()]


def get_symbols_missing_history() -> set[str]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol
                FROM stock_master
                WHERE history_initialized = FALSE
                ORDER BY symbol
                """
            )
            return {row[0] for row in cur.fetchall()}


def count_price_history_rows() -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM stock_price_history")
            return int(cur.fetchone()[0])


def get_price_history_summary() -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*),
                    MIN(trading_date),
                    MAX(trading_date),
                    COUNT(DISTINCT symbol)
                FROM stock_price_history
                """
            )
            row = cur.fetchone()
            return {
                "rows": int(row[0] or 0),
                "oldest_trading_date": row[1].isoformat() if row[1] else None,
                "latest_trading_date": row[2].isoformat() if row[2] else None,
                "symbols": int(row[3] or 0),
            }


def archive_news_headlines(headlines: Iterable[NewsHeadline]) -> int:
    rows = [row for row in headlines if row.headline and row.headline.strip()]
    if not rows:
        return 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO market_news_archive (
                    symbol,
                    headline,
                    fetch_time,
                    fetch_date,
                    source,
                    published_at,
                    url,
                    sentiment_score,
                    sentiment_method,
                    event_type,
                    volume,
                    p_change,
                    raw_payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, headline, fetch_date) WHERE btrim(headline) <> ''
                DO UPDATE SET
                    source = EXCLUDED.source,
                    published_at = COALESCE(EXCLUDED.published_at, market_news_archive.published_at),
                    url = COALESCE(EXCLUDED.url, market_news_archive.url),
                    sentiment_score = EXCLUDED.sentiment_score,
                    sentiment_method = EXCLUDED.sentiment_method,
                    event_type = EXCLUDED.event_type,
                    volume = EXCLUDED.volume,
                    p_change = EXCLUDED.p_change,
                    raw_payload = EXCLUDED.raw_payload
                """,
                [
                    (
                        row.symbol,
                        row.headline,
                        row.fetch_time,
                        row.fetch_time.date(),
                        row.source,
                        row.published_at,
                        row.url,
                        row.sentiment_score,
                        row.sentiment_method,
                        row.event_type,
                        row.volume,
                        row.p_change,
                        Jsonb(row.raw_payload or {}),
                    )
                    for row in rows
                ],
            )
        conn.commit()
    return len(rows)


def get_market_news_archive_summary() -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*),
                    COUNT(DISTINCT symbol),
                    MIN(fetch_time),
                    MAX(fetch_time),
                    MAX(published_at)
                FROM market_news_archive
                """
            )
            row = cur.fetchone()
            cur.execute(
                """
                SELECT symbol, headline, fetch_time, sentiment_score, sentiment_method, event_type
                FROM market_news_archive
                ORDER BY fetch_time DESC
                LIMIT 5
                """
            )
            recent = [
                {
                    "symbol": item[0],
                    "headline": item[1],
                    "fetch_time": item[2].isoformat() if item[2] else None,
                    "sentiment_score": item[3],
                    "sentiment_method": item[4],
                    "event_type": item[5],
                }
                for item in cur.fetchall()
            ]
            return {
                "rows": int(row[0] or 0),
                "symbols": int(row[1] or 0),
                "oldest_fetch_time": row[2].isoformat() if row[2] else None,
                "latest_fetch_time": row[3].isoformat() if row[3] else None,
                "latest_published_at": row[4].isoformat() if row[4] else None,
                "recent": recent,
            }


def get_bootstrap_state(key: str) -> str | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bootstrap_state WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None


def set_bootstrap_state(key: str, value: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bootstrap_state (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (key, value),
            )
        conn.commit()


def get_core_universe_previous_closes() -> dict[str, float]:
    """Returns a dict mapping yahoo_symbol to the latest close price from history for core universe stocks."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (sm.symbol) 
                    COALESCE(sm.yahoo_symbol, sm.symbol || '.NS'), 
                    sph.close
                FROM stock_master sm
                JOIN stock_price_history sph ON sm.symbol = sph.symbol
                WHERE sm.is_in_core_universe = TRUE
                ORDER BY sm.symbol, sph.trading_date DESC
                """
            )
            return {row[0]: float(row[1]) for row in cur.fetchall()}


def get_price_history_db(symbol: str, start_date: date) -> list[PriceCandle]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, trading_date, open, high, low, close, volume
                FROM stock_price_history
                WHERE symbol = %s AND trading_date >= %s
                ORDER BY trading_date ASC
                """,
                (symbol, start_date),
            )
            return [
                PriceCandle(
                    symbol=row[0],
                    trading_date=row[1],
                    open=row[2],
                    high=row[3],
                    low=row[4],
                    close=row[5],
                    volume=row[6],
                )
                for row in cur.fetchall()
            ]
