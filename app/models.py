from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Index, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class StockMaster(Base):
    __tablename__ = "stock_master"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    company_name: Mapped[str | None] = mapped_column(Text)
    exchange: Mapped[str | None] = mapped_column(Text)
    yahoo_symbol: Mapped[str | None] = mapped_column(Text)
    screener_url: Mapped[str | None] = mapped_column(Text)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    sector: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(Text)
    is_in_core_universe: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    history_initialized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_stock_master_symbol", "symbol"),
        Index("idx_stock_master_company_name", "company_name"),
        Index("idx_stock_master_core_universe", "is_in_core_universe"),
        Index("idx_stock_master_market_cap", "market_cap"),
    )


class StockPriceHistory(Base):
    __tablename__ = "stock_price_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_stock_price_history_symbol", "symbol"),
        Index("idx_stock_price_history_trading_date", "trading_date"),
        Index("idx_stock_price_history_symbol_date", "symbol", "trading_date", unique=True),
    )
