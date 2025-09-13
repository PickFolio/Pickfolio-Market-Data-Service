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

# --- Connection Manager for WebSockets ---
class ConnectionManager:
    def _init_(self):
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

# --- Constants ---
CONTEST_SERVICE_URL = "http://localhost:8081/api/internal/contests/active-symbols"

# --- Price Cache ---
last_known_prices: Dict[str, float] = {}


def is_market_open_india() -> bool:
    """
    Determines whether the Indian stock market is currently open.
    Market hours: Mon–Fri, 9:15 AM to 3:30 PM IST
    """
    india_tz = pytz.timezone("Asia/Kolkata")
    now = datetime.now(india_tz)
    market_open = datetime.strptime("09:15", "%H:%M").time()
    market_close = datetime.strptime("15:30", "%H:%M").time()
    return now.weekday() < 5 and market_open <= now.time() <= market_close


# --- Background Task for Price Fetching ---
async def broadcast_prices():
    global last_known_prices

    async with httpx.AsyncClient() as client:
        while True:
            try:
                # Step 1: Fetch active symbols
                response = await client.get(CONTEST_SERVICE_URL)
                if response.status_code != 200:
                    print(f"Error fetching symbols from contest-service: {response.status_code}")
                    await asyncio.sleep(15)
                    continue

                active_symbols: Set[str] = set(response.json())
                if not active_symbols:
                    print("No active symbols. Skipping.")
                    await asyncio.sleep(15)
                    continue

                broadcast_data = {}

                if is_market_open_india():
                    # ✅ Market is open: Always fetch fresh prices
                    print("Market is open. Fetching fresh prices for active symbols.")
                    tickers = yf.Tickers(" ".join(active_symbols))

                    for symbol in active_symbols:
                        try:
                            info = tickers.tickers[symbol].info
                            price = (
                                    info.get("currentPrice")
                                    or info.get("regularMarketPrice")
                                    or info.get("previousClose")
                            )
                            if price is not None:
                                last_known_prices[symbol] = price
                                broadcast_data[symbol] = price
                        except Exception as e:
                            print(f"Error fetching price for {symbol}: {e}")

                else:
                    # 🔒 Market is closed: Use cache if available, fetch only missing ones
                    print("Market is closed. Using cached prices where available.")
                    symbols_to_fetch = set()

                    for symbol in active_symbols:
                        cached_price = last_known_prices.get(symbol)
                        if cached_price is not None:
                            broadcast_data[symbol] = cached_price
                        else:
                            symbols_to_fetch.add(symbol)

                    if symbols_to_fetch:
                        print(f"Fetching missing prices: {symbols_to_fetch}")
                        tickers = yf.Tickers(" ".join(symbols_to_fetch))
                        for symbol in symbols_to_fetch:
                            try:
                                info = tickers.tickers[symbol].info
                                price = (
                                        info.get("currentPrice")
                                        or info.get("regularMarketPrice")
                                        or info.get("previousClose")
                                )
                                if price is not None:
                                    last_known_prices[symbol] = price
                                    broadcast_data[symbol] = price
                            except Exception as e:
                                print(f"Error fetching price for {symbol}: {e}")

                if broadcast_data:
                    await manager.broadcast(json.dumps(broadcast_data))
                    print(f"[{datetime.now()}] Broadcasted: {broadcast_data}")
                else:
                    print("Nothing to broadcast.")

            except httpx.RequestError as e:
                print(f"Could not connect to contest-service: {e}")
            except Exception as e:
                print(f"An error occurred in the broadcast loop: {e}")

            await asyncio.sleep(15)


# --- Lifespan Context Manager ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(broadcast_prices())
    print("Background price broadcasting task started.")
    yield


# --- FastAPI App and Endpoints ---
app = FastAPI(
    title="PickFolio Market Data Service",
    description="Provides stock market data for the PickFolio game.",
    version="1.0.0",
    lifespan=lifespan,
)

# --- Pydantic Models for API Responses ---

class QuoteResponse(BaseModel):
    symbol: str
    price: float

class ValidationResponse(BaseModel):
    symbol: str
    isValid: bool


@app.get("/api/market-data/validate/{symbol}", response_model=ValidationResponse, tags=["Market Data"])
def validate_symbol(symbol: str):
    ticker = yf.Ticker(symbol)
    if not ticker.info or ticker.info.get('regularMarketPrice') is None:
        return ValidationResponse(symbol=symbol, isValid=False)
    return ValidationResponse(symbol=symbol, isValid=True)


@app.get("/api/market-data/quote/{symbol}", response_model=QuoteResponse, tags=["Market Data"])
def get_quote(symbol: str):
    ticker = yf.Ticker(symbol)
    price = ticker.fast_info.get('last_price')

    if price is None:
        info = ticker.info
        price = info.get('regularMarketPrice')

    if price is None:
        raise HTTPException(status_code=404, detail=f"Price not found for symbol: {symbol}")
    return QuoteResponse(symbol=symbol, price=price)


@app.websocket("/ws/prices")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)