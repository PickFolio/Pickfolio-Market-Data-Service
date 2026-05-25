from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
