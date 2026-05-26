# PickFolio Market Data Service 📈

A lightweight, high-performance service dedicated to being the single source of truth for all stock market data. It fetches real-time and historical data from external sources and provides it to other PickFolio services via a simple REST API and a real-time WebSocket broadcast.

---

## Core Responsibilities

* **Stock Symbol Validation**: Provides an endpoint to verify if a given stock ticker is valid.
* **Price Quoting**: Provides an endpoint to get the latest price for a stock.
* **Real-time Price Broadcasting**:
    * Dynamically fetches the list of all "active" stock symbols from the Contest Service.
    * Polls `yfinance` for the latest prices of these symbols.
    * Implements a "market hours" optimization to reduce polling frequency when the market is closed.
    * Broadcasts the fetched prices to all connected clients (i.e., the Contest Service) via a WebSocket.

---

## Technology Stack

* **Framework**: FastAPI
* **Language**: Python 3.10+
* **Data Source**: yfinance
* **Real-time**: FastAPI WebSockets
* **HTTP Client**: httpx (for asynchronous inter-service communication)

---

## Local Development Setup

1.  **Clone the repository**:
    ```bash
    git clone <your-repo-url>
    cd pickfolio-market-data-service
    ```
2.  **Create and activate a virtual environment**:
    ```bash
    python -m venv venv
    source venv/bin/activate  # On macOS/Linux
    # .\venv\Scripts\activate    # On Windows
    ```
3.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
4.  **Run the application**:
    ```bash
    uvicorn main:app --reload --port 8082
    ```
    The service will be available at `http://localhost:8082`.

---

## API Endpoints

All endpoints are prefixed with `/api/market-data`.

| Method | Path | Description |
| :--- | :--- | :--- |
| **GET** | `/validate/{symbol}` | Validates if a stock symbol exists. |
| **GET** | `/quote/{symbol}` | Gets the latest price for a stock symbol. |
| **GET** | `/history/{symbol}` | Gets OHLCV chart history from yfinance. |
| **GET** | `/search?query={query}` | Searches Yahoo Finance for Indian stock symbols. |
| **GET** | `/pulse` | Gets cached market index pulse data. |
| **GET** | `/trending` | Gets cached top gainers and losers. |
| **POST** | `/catalysts` | Gets cached or freshly fetched stock catalysts. |
| **GET** | `/catalysts/top` | Gets top positive and negative catalysts. |
| **GET** | `/news/status` | Gets archive counts and latest news ingestion state. |
| **POST** | `/news/update` | Manually starts news ingestion from `stock_master`. |
| **GET** | `/health` | Health check. |

### WebSocket Endpoint (for internal services):

* `ws://localhost:8082/ws/market-data/prices`: The endpoint for clients (like the Contest Service) to connect to for receiving the real-time price broadcast.

---

## Historical Stock Storage

The service owns two long-lived PostgreSQL tables in `pickfolio_market_data`:

* `stock_master`: one row per discovered stock, including searchable metadata, core-universe flags, history initialization status, and raw Screener JSONB payload.
* `stock_price_history`: one row per symbol per trading day, with OHLCV candles and a unique constraint on `(symbol, trading_date)`.

Rows are not deleted automatically. If a stock leaves the core universe, update `stock_master.is_in_core_universe` to `false` but keep metadata and price history. Any stock discovered through future lazy loading should be inserted into `stock_master`, initialized with 1Y history, and then included in future incremental syncs.

## News Ingestion

News ingestion uses `stock_master` as its source of symbols. It runs daily at 07:00 IST by default, does not call NSE for the NIFTY Total Market, and does not fall back to hardcoded symbols. If `stock_master` is empty, the ingestion run records an empty universe instead of fetching unrelated symbols.

Full-universe intraday scanning is disabled by default because it competes with market movers and can overload small machines. Reactive catalyst fetches remain available through `/catalysts`. FinBERT sentiment scoring is also disabled by default; headline ingestion persists raw headlines first, and sentiment can be enabled later with `NEWS_SENTIMENT_ENABLED=true` or moved to a separate offline signal worker.

Successful yfinance news fetches are persisted to `market_news_archive`. This includes both scheduled batch ingestion and reactive `/catalysts` requests.

Manual trigger:

```bash
curl -X POST http://localhost:8082/api/market-data/news/update
curl -X POST "http://localhost:8082/api/market-data/news/update?limit=10"
```

Check status:

```bash
curl http://localhost:8082/api/market-data/news/status
```

Verify directly in PostgreSQL:

```sql
SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols, MAX(fetch_time) AS latest_fetch
FROM market_news_archive;

SELECT symbol, headline, fetch_time, sentiment_score
FROM market_news_archive
ORDER BY fetch_time DESC
LIMIT 10;
```

Schema migrations live in `migrations/` and are applied by the market-data service at startup and by bootstrap scripts before they write data.

## Automatic Bootstrap

On startup, the market-data service runs schema migrations and then checks `bootstrap_state`.

If the `stock_history_bootstrap_complete` marker is missing, the service starts the stock-history bootstrap in the background:

* fetch core universe from Screener
* store metadata in `stock_master`
* fetch 1Y daily candles from yfinance
* upsert candles into `stock_price_history`

When a full bootstrap finishes with no per-symbol failures, the service writes the completion marker. Later restarts skip the heavy bootstrap. If the VM restarts halfway through the first run, the marker will still be missing, so the next startup resumes safely through upserts.

The API still starts normally while the background bootstrap is running. Existing quote/history/search/live polling endpoints continue using yfinance directly, so current user flows are not blocked by bootstrap progress.

Disable automatic startup bootstrap if needed:

```bash
STOCK_BOOTSTRAP_ON_STARTUP=false
```

Limit automatic startup bootstrap for smoke testing:

```bash
STOCK_BOOTSTRAP_STARTUP_LIMIT=5
```

## Manual Bootstrap

Manual bootstrap is still available for retries, local testing, or forced refreshes. Run from the market-data service directory:

```bash
cd "Backend/Market Data Service"
python scripts/bootstrap_stock_history.py
```

Useful smoke-test options:

```bash
python scripts/bootstrap_stock_history.py --limit 5
python scripts/bootstrap_stock_history.py --limit 5 --skip-history
```

Run through Docker Compose from the Pickfolio root:

```bash
docker compose run --rm market-data-service python scripts/bootstrap_stock_history.py
```

On the Oracle VM:

```bash
cd /home/ubuntu/pickfolio
docker compose build market-data-service
docker compose run --rm market-data-service python scripts/bootstrap_stock_history.py
docker compose up -d --no-deps market-data-service
```

The script is safe to rerun. It uses PostgreSQL upserts for both metadata and candles, so interrupted runs continue without wiping data or creating duplicate `(symbol, trading_date)` rows.

## Incremental Daily History Updates

After bootstrap completes, the service refreshes recent daily candles for the active core universe every day at 03:00 IST by default. Startup catch-up is disabled by default so top gainers and losers can continue using the prior close for the rest of the market day after close. The updater fetches a short recent yfinance daily window and upserts into `stock_price_history`, so it is safe to rerun manually when needed.

Manual trigger through the running API:

```bash
curl -X POST http://localhost:8082/api/market-data/history/update
curl http://localhost:8082/api/market-data/history/status
```

Manual trigger through Docker Compose:

```bash
docker compose run --rm market-data-service python scripts/update_stock_history.py
```

Verify latest stored candles directly in PostgreSQL:

```sql
SELECT COUNT(*) AS rows, MAX(trading_date) AS latest, MIN(trading_date) AS oldest
FROM stock_price_history;

SELECT trading_date, COUNT(*)
FROM stock_price_history
GROUP BY trading_date
ORDER BY trading_date DESC
LIMIT 10;
```

## Bootstrap Environment Variables

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `DATABASE_URL` | `postgresql://pickfolio_user:pickfolio_pass@db:5432/pickfolio_market_data` | PostgreSQL connection string. |
| `SCREENER_QUERY_URL` | Screener raw query for market cap `> 1000` | URL template with `{page}` used to build the core universe. Override if Screener changes its query URL. |
| `SCREENER_MARKET_CAP_MIN_CR` | `1000` | Core-universe market cap threshold in crores. |
| `SCREENER_FETCH_COMPANY_DETAILS` | `true` | Fetch each Screener company detail page to store ratios, about text, and document links in `raw_metadata.detail`. |
| `SCREENER_REQUEST_DELAY_SEC` | `2.0` | Delay between Screener page requests. |
| `SCREENER_REQUEST_TIMEOUT_SEC` | `20.0` | Screener HTTP timeout. |
| `YFINANCE_HISTORY_PERIOD` | `1y` | Bootstrap yfinance period. |
| `YFINANCE_HISTORY_INTERVAL` | `1d` | Bootstrap yfinance interval. |
| `STOCK_BOOTSTRAP_ON_STARTUP` | `true` | Automatically run bootstrap in the background until the completion marker exists. |
| `STOCK_BOOTSTRAP_HISTORY_DELAY_SEC` | `1.0` | Delay between yfinance history calls during automatic bootstrap. |
| `STOCK_BOOTSTRAP_STARTUP_LIMIT` | unset | Optional max number of stocks for automatic bootstrap smoke tests. |
| `STOCK_HISTORY_INCREMENTAL_ON_STARTUP` | `false` | Run an incremental history catch-up when the service starts. Keep disabled to avoid refreshing closes immediately after a restart. |
| `STOCK_HISTORY_INCREMENTAL_SCHEDULER_ENABLED` | `true` | Enable the daily incremental history scheduler. |
| `STOCK_HISTORY_INCREMENTAL_RUN_HOUR_IST` | `3` | Daily scheduler hour in IST. |
| `STOCK_HISTORY_INCREMENTAL_RUN_MINUTE_IST` | `0` | Daily scheduler minute in IST. |
| `STOCK_HISTORY_INCREMENTAL_DELAY_SEC` | `0.5` | Delay between yfinance calls during incremental updates. |
| `STOCK_HISTORY_INCREMENTAL_LOOKBACK_DAYS` | `10` | Controls the recent yfinance period: `<=5` uses `5d`, otherwise `1mo`. |
| `STOCK_HISTORY_INCREMENTAL_STARTUP_LIMIT` | unset | Optional max symbols for startup catch-up smoke tests. |
