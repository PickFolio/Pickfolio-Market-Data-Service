import asyncio
import httpx
import yfinance as yf
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from typing import Dict, List, Set
from pydantic import BaseModel
from contextlib import asynccontextmanager
from datetime import datetime
import pytz
import json
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
            await connection.send_text(message)

manager = ConnectionManager()


CONTEST_SERVICE_URL = "http://localhost:8081/api/internal/contests/active-symbols"
last_known_prices: Dict[str, float] = {}
def is_market_open_india() -> bool:
    india_tz = pytz.timezone("Asia/Kolkata")
    now = datetime.now(india_tz)
    market_open = datetime.strptime("09:15", "%H:%M").time()
    market_close = datetime.strptime("15:30", "%H:%M").time()
    return now.weekday() < 5 and market_open <= now.time() <= market_close


# --- Helper function to isolate all blocking yfinance code ---
def fetch_prices_blocking(symbols: Set[str]) -> Dict[str, float]:
    """This function contains all the slow, blocking yfinance code. It will be run in a separate thread."""
    if not symbols:
        return {}
    logger.info(f"Executing blocking yfinance call for: {symbols}")
    prices = {}
    tickers = yf.Tickers(" ".join(symbols))
    for symbol in symbols:
        try:
            info = tickers.tickers[symbol].info
            price = (info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose"))
            if price is not None:
                prices[symbol] = price
        except Exception as e:
            logger.error(f"Error fetching yfinance price for {symbol}: {e}")
    logger.info("Blocking yfinance call finished.")
    return prices


# --- Background Task (Fully non-blocking and uses the global manager) ---
async def broadcast_prices():
    global last_known_prices
    async with httpx.AsyncClient() as client:
        while True:
            try:
                response = await client.get(CONTEST_SERVICE_URL)
                active_symbols: Set[str] = set(response.json()) if response.status_code == 200 and response.json() else set()

                if not active_symbols:
                    logger.info("No active symbols. Sleeping for 15s.")
                    await asyncio.sleep(15)
                    continue

                symbols_to_fetch = {s for s in active_symbols if is_market_open_india() or s not in last_known_prices}

                if symbols_to_fetch:
                    new_prices = await asyncio.to_thread(fetch_prices_blocking, symbols_to_fetch)
                    last_known_prices.update(new_prices)

                broadcast_data = {s: p for s, p in last_known_prices.items() if s in active_symbols}
                if broadcast_data:
                    await manager.broadcast(json.dumps(broadcast_data))
                    logger.info(f"Broadcasted: {broadcast_data}")

            except Exception as e:
                logger.error(f"An error occurred in the broadcast loop: {e}")
            await asyncio.sleep(15)


# --- Lifespan (Only starts the background task) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(broadcast_prices())
    yield


# --- FastAPI App ---
app = FastAPI(lifespan=lifespan)


# --- Pydantic Models (Unchanged) ---
class QuoteResponse(BaseModel):
    symbol: str
    price: float

class ValidationResponse(BaseModel):
    symbol: str
    isValid: bool


@app.get("/api/market-data/validate/{symbol}", response_model=ValidationResponse)
def validate_symbol(symbol: str):
    ticker = yf.Ticker(symbol)
    if not ticker.info or ticker.info.get('regularMarketPrice') is None:
        return ValidationResponse(symbol=symbol, isValid=False)
    return ValidationResponse(symbol=symbol, isValid=True)

@app.get("/api/market-data/quote/{symbol}", response_model=QuoteResponse)
def get_quote(symbol: str):
    ticker = yf.Ticker(symbol)
    price = ticker.fast_info.get('last_price')
    if price is None:
        info = ticker.info
        price = info.get('regularMarketPrice')
    if price is None:
        raise HTTPException(status_code=404, detail="Price not found")
    return QuoteResponse(symbol=symbol, price=price)


# --- WebSocket Endpoint ---
@app.websocket("/ws/market-data/prices")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

