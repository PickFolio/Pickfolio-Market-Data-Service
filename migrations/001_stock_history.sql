CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS stock_master (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL UNIQUE,
    company_name TEXT,
    exchange TEXT,
    yahoo_symbol TEXT,
    screener_url TEXT,
    market_cap NUMERIC(20, 2),
    sector TEXT,
    industry TEXT,
    is_in_core_universe BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    history_initialized BOOLEAN NOT NULL DEFAULT FALSE,
    raw_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stock_master_symbol ON stock_master (symbol);
CREATE INDEX IF NOT EXISTS idx_stock_master_company_name ON stock_master (company_name);
CREATE INDEX IF NOT EXISTS idx_stock_master_core_universe ON stock_master (is_in_core_universe);
CREATE INDEX IF NOT EXISTS idx_stock_master_market_cap ON stock_master (market_cap);
CREATE INDEX IF NOT EXISTS idx_stock_master_raw_metadata_gin ON stock_master USING GIN (raw_metadata);

DROP TRIGGER IF EXISTS trg_stock_master_updated_at ON stock_master;
CREATE TRIGGER trg_stock_master_updated_at
BEFORE UPDATE ON stock_master
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS stock_price_history (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL REFERENCES stock_master(symbol),
    trading_date DATE NOT NULL,
    open NUMERIC(20, 4) NOT NULL,
    high NUMERIC(20, 4) NOT NULL,
    low NUMERIC(20, 4) NOT NULL,
    close NUMERIC(20, 4) NOT NULL,
    volume BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_stock_price_history_symbol_date UNIQUE (symbol, trading_date)
);

CREATE INDEX IF NOT EXISTS idx_stock_price_history_symbol ON stock_price_history (symbol);
CREATE INDEX IF NOT EXISTS idx_stock_price_history_trading_date ON stock_price_history (trading_date);
CREATE INDEX IF NOT EXISTS idx_stock_price_history_symbol_trading_date
    ON stock_price_history (symbol, trading_date DESC);

DROP TRIGGER IF EXISTS trg_stock_price_history_updated_at ON stock_price_history;
CREATE TRIGGER trg_stock_price_history_updated_at
BEFORE UPDATE ON stock_price_history
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();
