CREATE TABLE IF NOT EXISTS market_news_archive (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    headline TEXT NOT NULL,
    fetch_time TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE market_news_archive
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'yfinance',
    ADD COLUMN IF NOT EXISTS fetch_date DATE NOT NULL DEFAULT CURRENT_DATE,
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS url TEXT,
    ADD COLUMN IF NOT EXISTS sentiment_score DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS sentiment_method TEXT,
    ADD COLUMN IF NOT EXISTS event_type TEXT,
    ADD COLUMN IF NOT EXISTS volume BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS p_change DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb;

DELETE FROM market_news_archive
WHERE headline IS NULL OR btrim(headline) = '';

DELETE FROM market_news_archive a
USING market_news_archive b
WHERE a.id < b.id
  AND a.symbol = b.symbol
  AND a.headline = b.headline
  AND a.fetch_date = b.fetch_date;

CREATE INDEX IF NOT EXISTS idx_market_news_archive_symbol
    ON market_news_archive (symbol);

CREATE INDEX IF NOT EXISTS idx_market_news_archive_fetch_time
    ON market_news_archive (fetch_time DESC);

CREATE INDEX IF NOT EXISTS idx_market_news_archive_published_at
    ON market_news_archive (published_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_market_news_archive_symbol_headline_day
    ON market_news_archive (symbol, headline, fetch_date)
    WHERE btrim(headline) <> '';
