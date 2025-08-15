from fastapi import FastAPI, HTTPException
from typing import Dict
import yfinance as yf
from pydantic import BaseModel

# Create an instance of the FastAPI class
app = FastAPI(
    title="PickFolio Market Data Service",
    description="Provides stock market data for the PickFolio game.",
    version="1.0.0"
)

# --- Pydantic Models for API Responses ---

class QuoteResponse(BaseModel):
    """
    Defines the structure for a stock quote response.
    """
    symbol: str
    price: float

class ValidationResponse(BaseModel):
    """
    Defines the structure for a symbol validation response.
    """
    symbol: str
    isValid: bool


# --- API Endpoints ---

@app.get("/", tags=["Health Check"])
def read_root() -> Dict[str, str]:
    """
    Root endpoint to check if the service is running.
    """
    return {"status": "Market Data Service is running"}


@app.get("/api/market-data/validate/{symbol}", response_model=ValidationResponse, tags=["Market Data"])
def validate_symbol(symbol: str):
    """
    Validates if a given stock symbol exists and has data.
    - **symbol**: The stock ticker to validate (e.g., 'RELIANCE.NS').
    """
    ticker = yf.Ticker(symbol)
    # yfinance returns an empty info dict for invalid tickers
    if not ticker.info or ticker.info.get('regularMarketPrice') is None:
        return ValidationResponse(symbol=symbol, isValid=False)
    return ValidationResponse(symbol=symbol, isValid=True)


@app.get("/api/market-data/quote/{symbol}", response_model=QuoteResponse, tags=["Market Data"])
def get_quote(symbol: str):
    """
    Gets the latest price for a given stock symbol.
    - **symbol**: The stock ticker to get a quote for (e.g., 'RELIANCE.NS').
    """
    ticker = yf.Ticker(symbol)
    # Use 'fast_info' for quicker price retrieval
    price = ticker.fast_info.get('last_price')

    if price is None:
        # Fallback to regular info if fast_info fails
        info = ticker.info
        price = info.get('regularMarketPrice')

    if price is None:
        raise HTTPException(
            status_code=404,
            detail=f"Invalid ticker: Price not found for symbol: {symbol}."
        )

    return QuoteResponse(symbol=symbol, price=price)