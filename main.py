import asyncio
import httpx
import yfinance as yf
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from typing import Dict, List, Set
from pydantic import BaseModel
from contextlib import asynccontextmanager

# --- Connection Manager for WebSockets ---
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

# --- Background Task for Price Fetching ---

# URL for the new endpoint on the contest-service
CONTEST_SERVICE_URL = "http://localhost:8081/api/internal/contests/active-symbols"

async def broadcast_prices():
    """
    A background task that fetches and broadcasts stock prices periodically.
    """
    active_symbols: Set[str] = set()

    async with httpx.AsyncClient() as client:
        while True:
            try:
                response = await client.get(CONTEST_SERVICE_URL)
                if response.status_code == 200:
                    symbols_from_service = response.json()
                    if symbols_from_service:
                        active_symbols = set(symbols_from_service)
                else:
                    print(f"Error fetching symbols from contest-service: {response.status_code}")
                    active_symbols = set()

                if active_symbols:
                    prices = {}
                    tickers = yf.Tickers(' '.join(active_symbols))
                    for symbol in active_symbols:
                        ticker_info = tickers.tickers[symbol].info

                        price = ticker_info.get('currentPrice') \
                                or ticker_info.get('regularMarketPrice') \
                                or ticker_info.get('previousClose')

                        if price:
                            prices[symbol] = price

                    if prices:
                        await manager.broadcast(str(prices).replace("'", '"'))
                else:
                    print("No active symbols to track.")

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

# --- FastAPI App and other endpoints ---
app = FastAPI(
    title="PickFolio Market Data Service",
    description="Provides stock market data for the PickFolio game.",
    version="1.0.0",
    lifespan=lifespan
)

# --- Pydantic Models for API Responses ---

class QuoteResponse(BaseModel):
    symbol: str
    price: float

class ValidationResponse(BaseModel):
    symbol: str
    isValid: bool


# --- API Endpoints ---


@app.get("/api/market-data/validate/{symbol}", response_model=ValidationResponse, tags=["Market Data"])
def validate_symbol(symbol: str):
    ticker = yf.Ticker(symbol)
    # yfinance returns an empty info dict for invalid tickers
    if not ticker.info or ticker.info.get('regularMarketPrice') is None:
        return ValidationResponse(symbol=symbol, isValid=False)
    return ValidationResponse(symbol=symbol, isValid=True)


@app.get("/api/market-data/quote/{symbol}", response_model=QuoteResponse, tags=["Market Data"])
def get_quote(symbol: str):
    ticker = yf.Ticker(symbol)
    # Use 'fast_info' for quicker price retrieval
    price = ticker.fast_info.get('last_price')

    if price is None:
        # Fallback to regular info if fast_info fails
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