import asyncio
import httpx
import yfinance as yf
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from typing import Dict, List, Set, Optional
from pydantic import BaseModel
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import pytz
import json
import logging
import os
import psycopg
import random
import gc
import concurrent.futures

# Dedicated executor for background polling tasks to prevent starving FastAPI's default executor
bg_executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)

async def run_bg(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(bg_executor, func, *args)

from psycopg.types.json import Jsonb
from app.bootstrap import run_stock_history_bootstrap_if_needed
from app.config import (
    STOCK_BOOTSTRAP_HISTORY_DELAY_SEC,
    STOCK_BOOTSTRAP_ON_STARTUP,
    STOCK_BOOTSTRAP_STARTUP_LIMIT,
)
from app.db import run_migrations

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- Pydantic Models ---
class PulseResponse(BaseModel):
    symbol: str
    move: str
    tone: str
    sparkline: List[float]

class Mover(BaseModel):
    symbol: str
    price: float
    change: float
    pChange: float

class CatalystResponse(BaseModel):
    symbol: str
    headline: str
    sentiment_score: float
    volume: float = 0.0
    pChange: float = 0.0

class TopCatalystsResponse(BaseModel):
    positive: List[CatalystResponse]
    negative: List[CatalystResponse]

class MarketMoversResponse(BaseModel):
    gainers: List[Mover]
    losers: List[Mover]

class QuoteResponse(BaseModel):
    symbol: str
    price: float
    changePercent: float = 0.0

class ValidationResponse(BaseModel):
    symbol: str
    isValid: bool

class SearchResult(BaseModel):
    symbol: str
    name: str
    type: str

class HistoricalDataPoint(BaseModel):
    time: str | int
    open: float
    high: float
    low: float
    close: float
    value: float

class CatalystsRequest(BaseModel):
    symbols: List[str]

class Catalyst(BaseModel):
    symbol: str
    headline: str
    fetch_time: str
    volume: float = 0.0
    pChange: float = 0.0


# --- Global State & Configuration ---
CONTEST_SERVICE_URL = os.environ.get("CONTEST_SERVICE_URL", "http://contest-service:8081/api/internal/contests/active-symbols")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://pickfolio_user:pickfolio_pass@db:5432/pickfolio_market_data")
PREMARKET_CACHE_FILE = "premarket_news_cache.json"

# Tunable Analytics Configs
MARKET_MOVERS_REFRESH_INTERVAL_SEC = int(os.environ.get("MARKET_MOVERS_REFRESH_INTERVAL_SEC", "1800"))
NEWS_INTRADAY_INTERVAL_SEC = int(os.environ.get("NEWS_INTRADAY_INTERVAL_SEC", "1800"))
NEWS_INTRADAY_DELAY_MIN = float(os.environ.get("NEWS_INTRADAY_DELAY_MIN", "1.0"))
NEWS_INTRADAY_DELAY_MAX = float(os.environ.get("NEWS_INTRADAY_DELAY_MAX", "1.5"))
NEWS_OVERNIGHT_DELAY_MIN = float(os.environ.get("NEWS_OVERNIGHT_DELAY_MIN", "1.5"))
NEWS_OVERNIGHT_DELAY_MAX = float(os.environ.get("NEWS_OVERNIGHT_DELAY_MAX", "2.5"))
NEWS_TOP_CATALYSTS_LIMIT = int(os.environ.get("NEWS_TOP_CATALYSTS_LIMIT", "10"))
NEWS_REACTIVE_FETCH_CONCURRENCY = int(os.environ.get("NEWS_REACTIVE_FETCH_CONCURRENCY", "2"))

NSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
}

last_known_prices: Dict[str, float] = {}
cached_pulse_data: List[PulseResponse] = [
    PulseResponse(symbol="NIFTY", move="0.00%", tone="var(--text-secondary)", sparkline=[]),
    PulseResponse(symbol="BANK", move="0.00%", tone="var(--text-secondary)", sparkline=[]),
    PulseResponse(symbol="IT", move="0.00%", tone="var(--text-secondary)", sparkline=[]),
]
cached_market_movers: MarketMoversResponse = MarketMoversResponse(gainers=[], losers=[])

# --- Connection Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()

def is_market_open_india() -> bool:
    india_tz = pytz.timezone("Asia/Kolkata")
    now = datetime.now(india_tz)
    market_open = datetime.strptime("09:15", "%H:%M").time()
    market_close = datetime.strptime("15:30", "%H:%M").time()
    return now.weekday() < 5 and market_open <= now.time() <= market_close

# --- Fetching Logic ---
def fetch_prices_blocking(symbols: Set[str]) -> Dict[str, float]:
    if not symbols:
        return {}
    prices = {}
    import pandas as pd
    sym_list = list(symbols)
    try:
        data = yf.download(sym_list, period="1d", group_by="ticker", auto_adjust=True, prepost=False, threads=True, progress=False)
        if isinstance(data.columns, pd.MultiIndex):
            for sym in sym_list:
                try:
                    val = data[sym]['Close'].iloc[-1]
                    if not pd.isna(val):
                        prices[sym] = float(val)
                except Exception:
                    pass
        else:
            if len(sym_list) == 1:
                try:
                    val = data['Close'].iloc[-1]
                    if not pd.isna(val):
                        prices[sym_list[0]] = float(val)
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Error in fetch_prices_blocking: {e}")
    return prices

def fetch_pulse_blocking() -> List[PulseResponse]:
    indices = {"^NSEI": "NIFTY", "^NSEBANK": "BANK", "^CNXIT": "IT"}
    results = []
    
    for symbol, name in indices.items():
        try:
            logger.info(f"Pulse: Fetching {symbol} ({name})")
            ticker = yf.Ticker(symbol)
            
            # Fetch daily data to get exact EOD closing prices
            hist_daily = ticker.history(period="5d", interval="1d")
            # Fetch intraday data strictly for the sparkline points
            hist_intra = ticker.history(period="5d", interval="15m")
            
            if hist_daily.empty or hist_intra.empty:
                results.append(PulseResponse(symbol=name, move="0.00%", tone="var(--text-secondary)", sparkline=[]))
                continue

            # Extract sparkline from the most recent day in the 15m intraday history
            days = sorted(list(set(hist_intra.index.date)))
            last_date = days[-1]
            today_hist = hist_intra[hist_intra.index.date == last_date]
            sparkline = [float(v) for v in today_hist['Close'].tolist()]
            
            change_percent = 0.0
            if len(hist_daily) >= 2:
                # Use daily interval data to get the true previous close
                prev_day_close = float(hist_daily['Close'].iloc[-2])
                current_price = float(hist_daily['Close'].iloc[-1])
                change_percent = ((current_price - prev_day_close) / prev_day_close) * 100
                
                # Prepend the previous close so the chart correctly visualizes the overnight gap
                sparkline.insert(0, prev_day_close)

            move_str = f"{'+' if change_percent >= 0 else ''}{change_percent:.2f}%"
            tone = 'var(--color-success)' if change_percent >= 0 else 'var(--color-error)'
            results.append(PulseResponse(symbol=name, move=move_str, tone=tone, sparkline=sparkline))
            logger.info(f"Pulse: Success for {name}")
        except Exception as e:
            results.append(PulseResponse(symbol=name, move="0.00%", tone="var(--text-secondary)", sparkline=[]))
    
    return results

# (fetch_market_movers_blocking removed to make it non-blocking async)

# --- Background Tasks ---
async def broadcast_prices_loop():
    global last_known_prices
    await asyncio.sleep(15)
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                response = await client.get(CONTEST_SERVICE_URL)
                if response.status_code == 200:
                    active_symbols = set(response.json() or [])
                    if active_symbols:
                        symbols_to_fetch = {s for s in active_symbols if is_market_open_india() or s not in last_known_prices}
                        if symbols_to_fetch:
                            try:
                                new_prices = await asyncio.wait_for(
                                    run_bg(fetch_prices_blocking, symbols_to_fetch),
                                    timeout=30.0
                                )
                                last_known_prices.update(new_prices)
                            except asyncio.TimeoutError:
                                pass

                        broadcast_data = {s: p for s, p in last_known_prices.items() if s in active_symbols}
                        if broadcast_data:
                            await manager.broadcast(json.dumps(broadcast_data))
                else:
                    logger.error(f"Active-symbols API returned status {response.status_code}")
            except Exception as e:
                logger.error(f"Error reaching active-symbols API: {e}")
                await asyncio.sleep(30)
                continue
            await asyncio.sleep(15)

async def update_pulse_cache_loop():
    global cached_pulse_data
    await asyncio.sleep(2)
    while True:
        try:
            new_pulse = await asyncio.wait_for(run_bg(fetch_pulse_blocking), timeout=30.0)
            if new_pulse:
                cached_pulse_data = new_pulse
        except Exception:
            pass
        await asyncio.sleep(300)

async def update_market_movers_cache_loop():
    global cached_market_movers
    await asyncio.sleep(4)
    while True:
        try:
            from app.repositories import get_core_universe_previous_closes
            import pandas as pd
            
            logger.info("Fetching market movers from core universe...")
            prev_closes = await run_bg(get_core_universe_previous_closes)
            if not prev_closes:
                logger.warning("No previous closes found for core universe.")
                await asyncio.sleep(MARKET_MOVERS_REFRESH_INTERVAL_SEC)
                continue

            symbols = list(prev_closes.keys())
            current_prices = {}
            
            chunk_size = 50
            total_chunks = (len(symbols) + chunk_size - 1) // chunk_size
            
            for i in range(0, len(symbols), chunk_size):
                chunk = symbols[i:i + chunk_size]
                current_chunk = (i // chunk_size) + 1
                logger.info(f"Fetching market movers chunk {current_chunk}/{total_chunks} ({len(chunk)} symbols)")
                
                try:
                    # Isolate yfinance entirely from our main background executor pool
                    data = await asyncio.to_thread(
                        yf.download, 
                        chunk, 
                        period="1d", 
                        group_by="ticker", 
                        auto_adjust=True, 
                        prepost=False, 
                        threads=False, # Disable yfinance internal threads
                        progress=False
                    )
                    
                    if isinstance(data.columns, pd.MultiIndex):
                        for sym in chunk:
                            try:
                                val = data[sym]['Close'].iloc[-1]
                                if not pd.isna(val):
                                    current_prices[sym] = float(val)
                            except KeyError:
                                try:
                                    val = data['Close'].iloc[-1]
                                    if not pd.isna(val):
                                        current_prices[sym] = float(val)
                                except Exception:
                                    pass
                            except Exception:
                                pass
                    else:
                        if len(chunk) == 1:
                            try:
                                val = data['Close'].iloc[-1]
                                if not pd.isna(val):
                                    current_prices[chunk[0]] = float(val)
                            except Exception:
                                pass
                except Exception as e:
                    logger.error(f"Error fetching chunk {current_chunk}: {e}")
                
                # Force yield back to the event loop so FastAPI can process network requests
                await asyncio.sleep(5.0)

            movers_list = []
            for sym, prev_close in prev_closes.items():
                curr_price = current_prices.get(sym)
                if curr_price is not None and prev_close > 0:
                    change = curr_price - prev_close
                    pChange = (change / prev_close) * 100
                    movers_list.append({
                        "symbol": sym,
                        "price": curr_price,
                        "change": change,
                        "pChange": pChange
                    })

            sorted_movers = sorted(movers_list, key=lambda x: x['pChange'], reverse=True)
            
            gainers = [Mover(symbol=m['symbol'], price=m['price'], change=m['change'], pChange=m['pChange']) for m in sorted_movers[:5]]
            losers = [Mover(symbol=m['symbol'], price=m['price'], change=m['change'], pChange=m['pChange']) for m in sorted_movers[-5:][::-1]]
            
            if gainers or losers:
                cached_market_movers = MarketMoversResponse(gainers=gainers, losers=losers)
                logger.info("Market movers cache successfully updated.")
                
        except Exception as e:
            logger.error(f"Market Movers Loop Error: {e}")
            
        await asyncio.sleep(MARKET_MOVERS_REFRESH_INTERVAL_SEC)

# --- News & Catalyst Logic ---
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://pickfolio_user:pickfolio_pass@db:5432/pickfolio_market_data")
cached_catalysts: Dict[str, Catalyst] = {}
PREMARKET_CACHE_FILE = "premarket_news_cache.json"

def init_db():
    if not DATABASE_URL: return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS market_news_archive (
                        id SERIAL PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        headline TEXT NOT NULL,
                        fetch_time TIMESTAMPTZ NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to initialize DB: {e}")

def archive_catalysts_to_db(catalysts: List[Catalyst]):
    if not DATABASE_URL or not catalysts: return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO market_news_archive (symbol, headline, fetch_time)
                    VALUES (%s, %s, %s)
                """, [(c.symbol, c.headline, c.fetch_time) for c in catalysts])
            conn.commit()
    except Exception:
        pass

def fetch_news_for_symbol(symbol: str) -> Optional[Catalyst]:
    try:
        ticker = yf.Ticker(symbol)
        news = ticker.news
        if news and len(news) > 0:
            headline = news[0].get("title", "")
            fast_info = ticker.fast_info
            volume = float(fast_info.last_volume) if hasattr(fast_info, 'last_volume') and fast_info.last_volume else 0.0
            last_price = float(fast_info.last_price) if hasattr(fast_info, 'last_price') and fast_info.last_price else 0.0
            prev_close = float(fast_info.previous_close) if hasattr(fast_info, 'previous_close') and fast_info.previous_close else 0.0
            pChange = ((last_price - prev_close) / prev_close * 100) if prev_close else 0.0

            return Catalyst(
                symbol=symbol,
                headline=headline,
                fetch_time=datetime.now(pytz.UTC).isoformat(),
                volume=volume,
                pChange=pChange
            )
    except Exception:
        pass
    return None

top_positive_catalysts: List[CatalystResponse] = []
top_negative_catalysts: List[CatalystResponse] = []
fetch_semaphore = asyncio.Semaphore(NEWS_REACTIVE_FETCH_CONCURRENCY)

def fetch_universe_symbols() -> List[str]:
    fallback_symbols = [
        "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
        "SBI.NS", "BHARTIARTL.NS", "ITC.NS", "HINDUNILVR.NS", "LT.NS"
    ]
    try:
        with httpx.Client(timeout=10.0, headers=NSE_HEADERS) as client:
            client.get('https://www.nseindia.com', timeout=10.0)
            res = client.get('https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20TOTAL%20MARKET', timeout=10.0)
            if res.status_code == 200:
                data = res.json().get('data', [])
                symbols = [d['symbol'] + '.NS' for d in data if d.get('symbol') and d.get('symbol') != 'NIFTY TOTAL MARKET']
                if symbols: return symbols
            else:
                logger.error(f"NSE API Error for Universe: Status {res.status_code}")
    except Exception as e:
        logger.error(f"Failed to fetch dynamic universe: {e}")
    return fallback_symbols

async def process_catalysts_batch(universe: List[str], is_intraday: bool = False):
    global top_positive_catalysts
    global top_negative_catalysts
    
    logger.info(f"Starting news batch processing for {len(universe)} symbols (Intraday: {is_intraday})...")
    logger.info("Loading FinBERT model into RAM...")
    try:
        from transformers import pipeline
        def load_pipeline():
            return pipeline("sentiment-analysis", model="ProsusAI/finbert")
        sentiment_pipeline = await run_bg(load_pipeline)
    except Exception as e:
        logger.error(f"Failed to load FinBERT: {e}")
        sentiment_pipeline = None

    new_catalysts = []
    positive_scored = []
    negative_scored = []
    
    for symbol in universe:
        catalyst = await run_bg(fetch_news_for_symbol, symbol)
        if catalyst:
            new_catalysts.append(catalyst)
            cached_catalysts[symbol] = catalyst
            
            sentiment_score = 0.0
            if sentiment_pipeline and catalyst.headline:
                try:
                    result_list = await run_bg(sentiment_pipeline, catalyst.headline)
                    result = result_list[0]
                    if result['label'] == 'positive':
                        sentiment_score = result['score']
                    elif result['label'] == 'negative':
                        sentiment_score = -result['score']
                except Exception:
                    pass

            c_resp = CatalystResponse(
                symbol=symbol, 
                headline=catalyst.headline, 
                sentiment_score=sentiment_score,
                volume=catalyst.volume,
                pChange=catalyst.pChange
            )
            
            if sentiment_score > 0:
                positive_scored.append(c_resp)
            elif sentiment_score < 0:
                negative_scored.append(c_resp)
                
        # Faster pacing for intraday (1.0-1.5s) vs overnight (1.5-2.5s)
        delay = random.uniform(1.0, 1.5) if is_intraday else random.uniform(1.5, 2.5)
        await asyncio.sleep(delay)
        
    logger.info(f"Batch complete. Evaluated {len(new_catalysts)} headlines. Found {len(positive_scored)} positive and {len(negative_scored)} negative catalysts.")
    logger.info("Unloading FinBERT model from RAM...")
    if sentiment_pipeline:
        del sentiment_pipeline
    gc.collect()
        
    positive_scored.sort(key=lambda x: (x.sentiment_score, abs(x.pChange), x.volume), reverse=True)
    negative_scored.sort(key=lambda x: (x.sentiment_score, abs(x.pChange), x.volume))
    
    top_positive_catalysts = positive_scored[:10]
    top_negative_catalysts = negative_scored[:10]
    
    try:
        with open(PREMARKET_CACHE_FILE, "w") as f:
            json.dump({k: v.model_dump() for k, v in cached_catalysts.items()}, f)
    except Exception:
        pass
        
    await run_bg(archive_catalysts_to_db, new_catalysts)


async def update_premarket_news_loop():
    logger.info("Starting Pre-Market News Primer loop...")
    await run_bg(init_db)
    
    global cached_catalysts
    if os.path.exists(PREMARKET_CACHE_FILE):
        try:
            with open(PREMARKET_CACHE_FILE, "r") as f:
                data = json.load(f)
                for k, v in data.items():
                    cached_catalysts[k] = Catalyst.model_validate(v)
        except Exception:
            pass
    
    while True:
        now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
        target_time = now_ist.replace(hour=8, minute=15, second=0, microsecond=0)
        if now_ist >= target_time:
            target_time += timedelta(days=1)
            
        wait_seconds = (target_time - now_ist).total_seconds()
        logger.info(f"Sleeping for {wait_seconds} seconds until next Pre-Market Primer at 08:15 AM IST.")
        await asyncio.sleep(wait_seconds)
        
        logger.info("Waking up: Initiating Pre-Market News Primer...")
        universe = await run_bg(fetch_universe_symbols)
        await process_catalysts_batch(universe, is_intraday=False)


async def intraday_news_scanner_loop():
    logger.info("Starting Intraday Breaking News Scanner loop...")
    await asyncio.sleep(60) # Offset start
    
    while True:
        if is_market_open_india():
            logger.info("Market is open. Initiating Intraday News Scanner for full universe...")
            universe = await run_bg(fetch_universe_symbols)
            # Process full universe, use faster pacing
            await process_catalysts_batch(universe, is_intraday=True)
        await asyncio.sleep(1800) # Run every 30 minutes during market hours


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrations_ok = False
    try:
        await run_bg(run_migrations)
        migrations_ok = True
    except Exception as e:
        logger.error(f"Failed to run market data migrations: {e}")

    if STOCK_BOOTSTRAP_ON_STARTUP and migrations_ok:
        bootstrap_limit = int(STOCK_BOOTSTRAP_STARTUP_LIMIT) if STOCK_BOOTSTRAP_STARTUP_LIMIT else None
        asyncio.create_task(
            asyncio.to_thread(
                run_stock_history_bootstrap_if_needed,
                limit=bootstrap_limit,
                history_delay=STOCK_BOOTSTRAP_HISTORY_DELAY_SEC,
            )
        )
    elif STOCK_BOOTSTRAP_ON_STARTUP:
        logger.warning("Stock history bootstrap skipped because migrations did not complete.")

    asyncio.create_task(broadcast_prices_loop())
    asyncio.create_task(update_pulse_cache_loop())
    asyncio.create_task(update_market_movers_cache_loop())
    asyncio.create_task(update_premarket_news_loop())
    asyncio.create_task(intraday_news_scanner_loop())
    yield

# --- FastAPI App ---
from fastapi.middleware.cors import CORSMiddleware
app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/api/market-data/catalysts", response_model=List[Catalyst])
async def get_catalysts(request: CatalystsRequest):
    results = []
    symbols_to_fetch = []
    now_utc = datetime.now(pytz.UTC)
    
    for symbol in request.symbols:
        cached = cached_catalysts.get(symbol)
        if cached:
            try:
                fetch_time = datetime.fromisoformat(cached.fetch_time)
                if (now_utc - fetch_time).total_seconds() > 7200:
                    symbols_to_fetch.append(symbol)
                else:
                    results.append(cached)
            except Exception:
                symbols_to_fetch.append(symbol)
        else:
            symbols_to_fetch.append(symbol)
            
    if symbols_to_fetch:
        logger.info(f"Intraday Reactive Fetch triggered for {len(symbols_to_fetch)} symbols.")
        for symbol in symbols_to_fetch:
            async with fetch_semaphore:
                catalyst = await run_bg(fetch_news_for_symbol, symbol)
                await asyncio.sleep(0.5) # Rate limit intra-fetch
                if catalyst:
                    cached_catalysts[symbol] = catalyst
                    results.append(catalyst)
                    try:
                        with open(PREMARKET_CACHE_FILE, "w") as f:
                            json.dump({k: v.model_dump() for k, v in cached_catalysts.items()}, f)
                    except Exception:
                        pass
    return results

@app.get("/api/market-data/health")
async def health_check():
    return {"status": "ok", "cache_ready": len(cached_pulse_data) > 0}

@app.get("/api/market-data/pulse", response_model=List[PulseResponse])
async def get_market_pulse():
    return cached_pulse_data

@app.get("/api/market-data/trending", response_model=MarketMoversResponse)
async def get_trending():
    return MarketMoversResponse(gainers=cached_market_movers.gainers, losers=cached_market_movers.losers)

@app.get("/api/market-data/catalysts/top", response_model=TopCatalystsResponse)
async def get_top_catalysts():
    return TopCatalystsResponse(positive=top_positive_catalysts, negative=top_negative_catalysts)

def _calculate_start_date(range_str: str):
    now = datetime.now(pytz.timezone("Asia/Kolkata")).date()
    if range_str == "1d": return now - timedelta(days=1)
    elif range_str == "5d": return now - timedelta(days=7)
    elif range_str == "1mo": return now - timedelta(days=30)
    elif range_str == "3mo": return now - timedelta(days=90)
    elif range_str == "6mo": return now - timedelta(days=180)
    elif range_str == "1y": return now - timedelta(days=365)
    elif range_str == "2y": return now - timedelta(days=730)
    elif range_str == "5y": return now - timedelta(days=365 * 5)
    elif range_str == "10y": return now - timedelta(days=365 * 10)
    elif range_str == "ytd": return now.replace(month=1, day=1)
    elif range_str == "max": return now.replace(year=1920, month=1, day=1)
    return None

@app.get("/api/market-data/history/{symbol}")
async def get_stock_history(symbol: str, range: str = "1mo", interval: str = "1d"):
    try:
        from app.repositories import get_price_history_db, upsert_price_history, PriceCandle
        import pandas as pd
        
        # 1. Try Database if interval is supported
        start_date = None
        if interval == "1d":
            start_date = _calculate_start_date(range)
            if start_date:
                db_candles = await run_bg(get_price_history_db, symbol, start_date)
                
                if db_candles:
                    today = datetime.now(pytz.timezone("Asia/Kolkata")).date()
                    oldest_db_date = db_candles[0].trading_date
                    newest_db_date = db_candles[-1].trading_date
                    
                    # Staleness check: missing the last few days? (allowing 5 days for long weekends/holidays)
                    is_fresh = (today - newest_db_date).days <= 5
                    
                    # Completeness check: does it go far back enough? (allowing 7 days grace)
                    # Skip completeness check for max/10y to prevent infinite fallback on IPOs
                    is_complete = range in ["max", "10y", "5y", "2y"] or (oldest_db_date - start_date).days <= 7
                    
                    if is_fresh and is_complete:
                        logger.info(f"[CHART HISTORY] DB HIT: Served for {symbol} (range: {range})")
                        return [
                            {
                                "time": int(datetime(candle.trading_date.year, candle.trading_date.month, candle.trading_date.day, tzinfo=pytz.timezone("Asia/Kolkata")).timestamp()),
                                "open": float(candle.open),
                                "high": float(candle.high),
                                "low": float(candle.low),
                                "close": float(candle.close),
                                "value": float(candle.volume)
                            }
                            for candle in db_candles
                        ]
                    else:
                        reason = "stale data" if not is_fresh else "incomplete history"
                        logger.info(f"[CHART HISTORY] DB MISS: {symbol} has {reason}. Falling back to yfinance.")

        # 2. Fallback to yfinance
        logger.info(f"[CHART HISTORY] Fallback fetch: {symbol} (requested range: {range}, interval: {interval})")
        ticker = yf.Ticker(symbol)
        
        # Self-healing: if daily chart, fetch '1y' (max UI requirement) to fix the database permanently
        fetch_period = "1y" if interval == "1d" else range
        
        # If user explicitly requested more than 1y, we must fetch what they asked for
        if range in ["2y", "5y", "10y", "max", "ytd"]:
             fetch_period = range
             
        df = await asyncio.wait_for(asyncio.to_thread(ticker.history, period=fetch_period, interval=interval), timeout=15.0)
        
        if df.empty: return []
        
        # 3. Opportunistically save full fallback data to DB
        if interval == "1d" and not df.empty:
            candles_to_save = []
            for index, row in df.iterrows():
                try:
                    trading_date = index.date()
                    candles_to_save.append(PriceCandle(
                        symbol=symbol,
                        trading_date=trading_date,
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=float(row["Close"]),
                        volume=int(row["Volume"])
                    ))
                except Exception:
                    pass
            if candles_to_save:
                logger.info(f"[CHART HISTORY] Self-healing: Upserting {len(candles_to_save)} candles to DB for {symbol}")
                asyncio.create_task(run_bg(upsert_price_history, candles_to_save))

        # Filter the fetched dataframe to the user's requested range
        if interval == "1d" and start_date:
            df = df[df.index.strftime('%Y-%m-%d') >= start_date.strftime('%Y-%m-%d')]

        results = [
            {
                "time": int(index.timestamp()),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "value": float(row["Volume"])
            }
            for index, row in df.iterrows()
        ]

        return results
    except Exception as e:
        logger.error(f"[CHART HISTORY] Error fetching chart history for {symbol}: {e}")
        return []

@app.get("/api/market-data/quote/{symbol}", response_model=QuoteResponse)
async def get_quote(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        hist = await asyncio.wait_for(asyncio.to_thread(ticker.history, period="5d", interval="1d"), timeout=10.0)
        if hist.empty: raise HTTPException(status_code=404, detail="Price not found")
        current_price = float(hist['Close'].iloc[-1])
        change_percent = 0.0
        if len(hist) >= 2:
            prev_close = float(hist['Close'].iloc[-2])
            change_percent = ((current_price - prev_close) / prev_close) * 100
        return QuoteResponse(symbol=symbol, price=current_price, changePercent=change_percent)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/market-data/validate/{symbol}", response_model=ValidationResponse)
async def validate_symbol(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        await asyncio.wait_for(asyncio.to_thread(lambda: ticker.fast_info['last_price']), timeout=5.0)
        return ValidationResponse(symbol=symbol, isValid=True)
    except Exception:
        return ValidationResponse(symbol=symbol, isValid=False)

@app.get("/api/market-data/search", response_model=List[SearchResult])
async def search_stocks(query: str):
    if not query: return []
    url = "https://query2.finance.yahoo.com/v1/finance/search"
    params = {"q": query, "quotesCount": 20, "region": "IN", "lang": "en-IN"}
    headers = {'User-Agent': 'Mozilla/5.0'}
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            response = await client.get(url, params=params, headers=headers)
            data = response.json()
            results = []
            for quote in data.get("quotes", []):
                symbol = quote.get("symbol", "")
                if ".NS" in symbol or ".BO" in symbol:
                    results.append(SearchResult(symbol=symbol, name=quote.get("longname") or quote.get("shortname") or symbol, type=quote.get("quoteType", "Unknown")))
            return results
        except Exception:
            return []

@app.websocket("/ws/market-data/prices")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        manager.disconnect(websocket)
