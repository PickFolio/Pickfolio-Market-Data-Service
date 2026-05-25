from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Iterable

import pandas as pd
import yfinance as yf

from .config import YFINANCE_HISTORY_INTERVAL, YFINANCE_HISTORY_PERIOD
from .repositories import PriceCandle


def normalize_yahoo_symbol(symbol: str, default_exchange_suffix: str = ".NS") -> str:
    normalized = symbol.strip().upper()
    if normalized.endswith(".NS") or normalized.endswith(".BO"):
        return normalized
    return f"{normalized}{default_exchange_suffix}"


def candidate_yahoo_symbols(symbol: str) -> list[str]:
    normalized = symbol.strip().upper()
    if normalized.endswith(".NS") or normalized.endswith(".BO"):
        return [normalized]
    return [f"{normalized}.NS", f"{normalized}.BO"]


def _decimal(value) -> Decimal | None:
    if pd.isna(value):
        return None
    try:
        return Decimal(str(float(value))).quantize(Decimal("0.0001"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def dataframe_to_candles(symbol: str, df: pd.DataFrame) -> list[PriceCandle]:
    candles: list[PriceCandle] = []
    if df.empty:
        return candles

    for index, row in df.iterrows():
        open_price = _decimal(row.get("Open"))
        high_price = _decimal(row.get("High"))
        low_price = _decimal(row.get("Low"))
        close_price = _decimal(row.get("Close"))
        if None in (open_price, high_price, low_price, close_price):
            continue

        trading_date = index.date()
        volume = row.get("Volume", 0)
        candles.append(
            PriceCandle(
                symbol=symbol,
                trading_date=trading_date,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=int(volume) if not pd.isna(volume) else 0,
            )
        )

    return candles


def fetch_daily_history(
    symbol: str,
    period: str = YFINANCE_HISTORY_PERIOD,
    interval: str = YFINANCE_HISTORY_INTERVAL,
) -> tuple[str, list[PriceCandle]]:
    last_error: Exception | None = None
    for yahoo_symbol in candidate_yahoo_symbols(symbol):
        try:
            ticker = yf.Ticker(yahoo_symbol)
            df = ticker.history(period=period, interval=interval)
            candles = dataframe_to_candles(yahoo_symbol, df)
            if candles:
                return yahoo_symbol, candles
        except Exception as exc:
            last_error = exc

    if last_error:
        raise last_error
    return normalize_yahoo_symbol(symbol), []


def unique_symbols(symbols: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for symbol in symbols:
        normalized = symbol.strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result
