import asyncio
import httpx
import yfinance as yf
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from typing import Dict, List, Set, Optional
from pydantic import BaseModel
from contextlib import asynccontextmanager
from datetime import datetime
import pytz
import json
import logging
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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

class MarketMoversResponse(BaseModel):
    gainers: List[Mover]
    losers: List[Mover]

class QuoteResponse(BaseModel):
    symbol: str
    price: float

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

# --- Global State ---
# The URL must resolve inside the docker network
CONTEST_SERVICE_URL = os.environ.get("CONTEST_SERVICE_URL", "http://contest-service:8081/api/internal/contests/active-symbols")
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
    logger.info(f"Fetching prices for: {symbols}")
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1d")
            if not hist.empty:
                prices[symbol] = float(hist['Close'].iloc[-1])
        except Exception as e:
            logger.error(f"Error fetching price for {symbol}: {e}")
    return prices

def fetch_pulse_blocking() -> List[PulseResponse]:
    indices = {"^NSEI": "NIFTY", "^NSEBANK": "BANK", "^CNXIT": "IT"}
    results = []
    logger.info("Starting pulse fetch...")
    
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
            logger.error(f"Pulse: Failed {name}: {e}")
            results.append(PulseResponse(symbol=name, move="0.00%", tone="var(--text-secondary)", sparkline=[]))
    
    return results

def fetch_market_movers_blocking() -> MarketMoversResponse:
    logger.info("Fetching market movers from NSE NIFTY 500...")
    try:
        with httpx.Client(timeout=15.0, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}) as client:
            client.get('https://www.nseindia.com')
            
            res = client.get('https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500')
            
            if res.status_code != 200:
                raise Exception(f"NSE API Error: {res.status_code}")
                
            data = res.json().get('data', [])
            
            # Filter out the index itself
            stocks = [d for d in data if d.get('symbol') != 'NIFTY 500']
            
            # Sort by percentage change (highest first)
            sorted_stocks = sorted(stocks, key=lambda x: float(x.get('pChange', 0)), reverse=True)
            
            gainers = []
            for g in sorted_stocks[:5]:
                ltp = float(g.get('lastPrice') or 0)
                change = float(g.get('change') or 0)
                pChange = float(g.get('pChange') or 0)
                gainers.append(Mover(symbol=g['symbol'] + '.NS', price=ltp, change=change, pChange=pChange))
            
            # Bottom 5 (lowest negative change first)
            losers = []
            for l in sorted_stocks[-5:][::-1]:
                ltp = float(l.get('lastPrice') or 0)
                change = float(l.get('change') or 0)
                pChange = float(l.get('pChange') or 0)
                losers.append(Mover(symbol=l['symbol'] + '.NS', price=ltp, change=change, pChange=pChange))
            
            return MarketMoversResponse(gainers=gainers, losers=losers)
    except Exception as e:
        logger.error(f"Error fetching market movers: {e}")
        return MarketMoversResponse(gainers=[], losers=[])

# --- Background Tasks ---
async def broadcast_prices_loop():
    global last_known_prices
    logger.info("Starting broadcast prices loop...")
    await asyncio.sleep(5)
    
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
                                    asyncio.to_thread(fetch_prices_blocking, symbols_to_fetch),
                                    timeout=30.0
                                )
                                last_known_prices.update(new_prices)
                            except asyncio.TimeoutError:
                                logger.warning("Price fetch timed out")

                        broadcast_data = {s: p for s, p in last_known_prices.items() if s in active_symbols}
                        if broadcast_data:
                            await manager.broadcast(json.dumps(broadcast_data))
            except Exception as e:
                logger.error(f"Error in broadcast loop: {e}")
            await asyncio.sleep(15)

async def update_pulse_cache_loop():
    global cached_pulse_data
    logger.info("Starting pulse cache loop...")
    await asyncio.sleep(2)

    while True:
        try:
            logger.info("Initiating pulse cache update...")
            try:
                new_pulse = await asyncio.wait_for(
                    asyncio.to_thread(fetch_pulse_blocking),
                    timeout=30.0
                )
                if new_pulse:
                    cached_pulse_data = new_pulse
                    logger.info("Pulse cache updated successfully.")
            except asyncio.TimeoutError:
                logger.warning("Pulse fetch timed out")
        except Exception as e:
            logger.error(f"Error in pulse cache loop: {e}")
        await asyncio.sleep(300)

async def update_market_movers_cache_loop():
    global cached_market_movers
    logger.info("Starting market movers cache loop...")
    await asyncio.sleep(4)

    while True:
        try:
            logger.info("Initiating market movers cache update...")
            try:
                new_movers = await asyncio.wait_for(
                    asyncio.to_thread(fetch_market_movers_blocking),
                    timeout=30.0
                )
                if new_movers and (new_movers.gainers or new_movers.losers):
                    cached_market_movers = new_movers
                    logger.info("Market movers cache updated successfully.")
            except asyncio.TimeoutError:
                logger.warning("Market movers fetch timed out")
        except Exception as e:
            logger.error(f"Error in market movers cache loop: {e}")
        # Update hourly (3600 seconds) since we don't want to overburden NSE API
        await asyncio.sleep(3600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(broadcast_prices_loop())
    asyncio.create_task(update_pulse_cache_loop())
    asyncio.create_task(update_market_movers_cache_loop())
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

@app.get("/api/market-data/health")
async def health_check():
    return {"status": "ok", "cache_ready": len(cached_pulse_data) > 0}

@app.get("/api/market-data/pulse", response_model=List[PulseResponse])
async def get_market_pulse():
    return cached_pulse_data

@app.get("/api/market-data/trending", response_model=MarketMoversResponse)
async def get_trending():
    return cached_market_movers

@app.get("/api/market-data/history/{symbol}")
async def get_stock_history(symbol: str, range: str = "1mo", interval: str = "1d"):
    try:
        ticker = yf.Ticker(symbol)
        df = await asyncio.wait_for(
            asyncio.to_thread(ticker.history, period=range, interval=interval),
            timeout=15.0
        )
        if df.empty: return []
        return [
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
    except Exception as e:
        logger.error(f"History fetch failed for {symbol}: {e}")
        return []

@app.get("/api/market-data/quote/{symbol}", response_model=QuoteResponse)
async def get_quote(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        hist = await asyncio.wait_for(
            asyncio.to_thread(ticker.history, period="1d"),
            timeout=10.0
        )
        if hist.empty:
            raise HTTPException(status_code=404, detail="Price not found")
        return QuoteResponse(symbol=symbol, price=float(hist['Close'].iloc[-1]))
    except Exception as e:
        logger.error(f"Quote fetch failed for {symbol}: {e}")
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
                    results.append(SearchResult(
                        symbol=symbol,
                        name=quote.get("longname") or quote.get("shortname") or symbol,
                        type=quote.get("quoteType", "Unknown")
                    ))
            return results
        except Exception:
            return []

@app.websocket("/ws/market-data/prices")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)