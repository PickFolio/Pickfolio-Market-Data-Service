import os
from pathlib import Path


def load_dotenv_if_present() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv_if_present()


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://pickfolio_user:pickfolio_pass@db:5432/pickfolio_market_data",
)

SCREENER_BASE_URL = os.environ.get("SCREENER_BASE_URL", "https://www.screener.in")
SCREENER_QUERY_URL = os.environ.get(
    "SCREENER_QUERY_URL",
    f"{SCREENER_BASE_URL}/screens/1473147/market-cap-above-1000-crore/?order=desc&page={{page}}",
)
SCREENER_MARKET_CAP_MIN_CR = float(os.environ.get("SCREENER_MARKET_CAP_MIN_CR", "1000"))
SCREENER_REQUEST_DELAY_SEC = float(os.environ.get("SCREENER_REQUEST_DELAY_SEC", "2.0"))
SCREENER_REQUEST_TIMEOUT_SEC = float(os.environ.get("SCREENER_REQUEST_TIMEOUT_SEC", "20.0"))
SCREENER_FETCH_COMPANY_DETAILS = os.environ.get("SCREENER_FETCH_COMPANY_DETAILS", "true").lower() == "true"

YFINANCE_HISTORY_PERIOD = os.environ.get("YFINANCE_HISTORY_PERIOD", "1y")
YFINANCE_HISTORY_INTERVAL = os.environ.get("YFINANCE_HISTORY_INTERVAL", "1d")

STOCK_BOOTSTRAP_ON_STARTUP = os.environ.get("STOCK_BOOTSTRAP_ON_STARTUP", "true").lower() == "true"
STOCK_BOOTSTRAP_HISTORY_DELAY_SEC = float(os.environ.get("STOCK_BOOTSTRAP_HISTORY_DELAY_SEC", "1.0"))
STOCK_BOOTSTRAP_STARTUP_LIMIT = os.environ.get("STOCK_BOOTSTRAP_STARTUP_LIMIT")
