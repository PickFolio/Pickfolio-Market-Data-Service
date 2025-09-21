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

### WebSocket Endpoint (for internal services):

* `ws://localhost:8082/ws/market-data/prices`: The endpoint for clients (like the Contest Service) to connect to for receiving the real-time price broadcast.
