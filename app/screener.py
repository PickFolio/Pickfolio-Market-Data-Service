from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .config import (
    SCREENER_BASE_URL,
    SCREENER_FETCH_COMPANY_DETAILS,
    SCREENER_MARKET_CAP_MIN_CR,
    SCREENER_QUERY_URL,
    SCREENER_REQUEST_DELAY_SEC,
    SCREENER_REQUEST_TIMEOUT_SEC,
)


logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass(frozen=True)
class ScreenerCompany:
    symbol: str
    company_name: str | None
    market_cap: Decimal | None
    sector: str | None
    industry: str | None
    screener_url: str | None
    raw_metadata: dict[str, Any]


def company_detail_url(symbol: str) -> str:
    return f"{SCREENER_BASE_URL}/company/{symbol}/consolidated/"


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def _decimal_from_text(value: str | None) -> Decimal | None:
    if not value:
        return None
    normalized = value.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", normalized)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _normalize_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def _symbol_from_company_href(href: str | None) -> str | None:
    if not href:
        return None
    match = re.search(r"/company/([^/]+)/?", href)
    if not match:
        return None
    symbol = match.group(1).strip().upper()
    if symbol.endswith("-CONSOLIDATED"):
        symbol = symbol.removesuffix("-CONSOLIDATED")
    return symbol or None


def _find_market_cap(raw: dict[str, Any]) -> Decimal | None:
    for key, value in raw.items():
        if ("market" in key and "cap" in key) or "mar_cap" in key or "mcap" in key:
            return _decimal_from_text(str(value))
    return None


def _find_name(raw: dict[str, Any]) -> str | None:
    for key in ("name", "company", "company_name"):
        if raw.get(key):
            return _clean(str(raw[key]))
    return None


def _parse_company_rows(html: str) -> list[ScreenerCompany]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    header_row = table.find("tr")
    headers = []
    if header_row:
        headers = [_normalize_header(th.get_text(" ", strip=True)) for th in header_row.find_all("th")]
    companies: list[ScreenerCompany] = []

    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if not cells:
            continue

        values = [_clean(cell.get_text(" ", strip=True)) for cell in cells]
        raw = {
            headers[index] if index < len(headers) and headers[index] else f"column_{index + 1}": value
            for index, value in enumerate(values)
        }

        link = row.find("a", href=re.compile(r"/company/"))
        symbol = _symbol_from_company_href(link.get("href") if link else None)
        if not symbol:
            continue

        screener_url = urljoin(SCREENER_BASE_URL, link.get("href")) if link else None
        company_name = _clean(link.get_text(" ", strip=True)) if link else _find_name(raw)
        market_cap = _find_market_cap(raw)

        raw["screener_symbol"] = symbol
        raw["screener_url"] = screener_url

        companies.append(
            ScreenerCompany(
                symbol=symbol,
                company_name=company_name,
                market_cap=market_cap,
                sector=None,
                industry=None,
                screener_url=screener_url,
                raw_metadata=raw,
            )
        )

    return companies


def _find_total_pages(html: str) -> int | None:
    match = re.search(r"Showing page\s+\d+\s+of\s+(\d+)", html, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _extract_ratio_cards(soup: BeautifulSoup) -> dict[str, str]:
    ratios: dict[str, str] = {}
    for item in soup.select("ul#top-ratios li, .company-ratios li"):
        name_node = item.select_one(".name")
        value_node = item.select_one(".value")
        name = _clean(name_node.get_text(" ", strip=True) if name_node else None)
        value = _clean(value_node.get_text(" ", strip=True) if value_node else None)
        if name and value:
            ratios[_normalize_header(name)] = value
    return ratios


def _extract_documents(soup: BeautifulSoup, section_class_fragment: str) -> list[dict[str, str | None]]:
    section = soup.find("div", class_=lambda value: value and section_class_fragment in value)
    if not section:
        return []

    documents: list[dict[str, str | None]] = []
    for row in section.find_all("li"):
        date_node = row.find("div", class_=lambda value: value and "nowrap" in value)
        date = _clean(date_node.get_text(" ", strip=True) if date_node else None)
        for link in row.find_all("a", href=True):
            title = _clean(link.get_text(" ", strip=True))
            href = link["href"]
            if not title or not href:
                continue
            documents.append(
                {
                    "title": title,
                    "url": href if href.startswith("http") else urljoin(SCREENER_BASE_URL, href),
                    "type": "pdf" if href.lower().endswith(".pdf") else "document",
                    "date": date,
                }
            )
    return documents


def _extract_about_text(soup: BeautifulSoup) -> str | None:
    about = soup.find(id="company-info")
    if about:
        return _clean(about.get_text(" ", strip=True))

    for heading in soup.find_all(["h2", "h3"]):
        if "about" not in heading.get_text(" ", strip=True).lower():
            continue
        sibling = heading.find_next_sibling()
        if sibling:
            return _clean(sibling.get_text(" ", strip=True))
    return None


def _extract_sector_industry(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    page_text = soup.get_text(" ", strip=True)
    sector_match = re.search(r"Sector\s*[:\-]\s*([A-Za-z &/-]+)", page_text)
    industry_match = re.search(r"Industry\s*[:\-]\s*([A-Za-z &/-]+)", page_text)
    sector = _clean(sector_match.group(1)) if sector_match else None
    industry = _clean(industry_match.group(1)) if industry_match else None
    return sector, industry


def fetch_company_detail(symbol: str) -> dict[str, Any]:
    if not SCREENER_FETCH_COMPANY_DETAILS:
        return {}

    url = company_detail_url(symbol)
    try:
        with httpx.Client(timeout=SCREENER_REQUEST_TIMEOUT_SEC, headers=HEADERS, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to fetch Screener detail page for %s: %s", symbol, exc)
        return {"detail_url": url, "detail_fetch_error": str(exc)}

    soup = BeautifulSoup(response.text, "html.parser")
    h1 = soup.find("h1")
    sector, industry = _extract_sector_industry(soup)

    return {
        "detail_url": url,
        "company_name": _clean(h1.get_text(" ", strip=True) if h1 else None),
        "about": _extract_about_text(soup),
        "sector": sector,
        "industry": industry,
        "ratios": _extract_ratio_cards(soup),
        "documents": {
            "concalls": _extract_documents(soup, "concalls"),
            "credit_ratings": _extract_documents(soup, "credit-ratings"),
            "annual_reports": _extract_documents(soup, "annual-reports"),
        },
    }


def fetch_core_universe() -> list[ScreenerCompany]:
    companies_by_symbol: dict[str, ScreenerCompany] = {}
    page = 1

    with httpx.Client(timeout=SCREENER_REQUEST_TIMEOUT_SEC, headers=HEADERS, follow_redirects=True) as client:
        while True:
            url = SCREENER_QUERY_URL.format(page=page)
            logger.info("Fetching Screener page %s: %s", page, url)
            response = client.get(url)
            response.raise_for_status()

            total_pages = _find_total_pages(response.text)
            if total_pages is not None and page > total_pages:
                logger.info("Stopping Screener pagination at page %s; total pages is %s", page, total_pages)
                break

            page_companies = _parse_company_rows(response.text)
            eligible = [
                company
                for company in page_companies
                if company.market_cap is not None and company.market_cap >= Decimal(str(SCREENER_MARKET_CAP_MIN_CR))
            ]

            for company in eligible:
                companies_by_symbol[company.symbol] = company

            logger.info("Parsed %s rows from Screener page %s", len(page_companies), page)
            if not page_companies:
                break
            if total_pages is not None and page >= total_pages:
                break

            page += 1
            time.sleep(SCREENER_REQUEST_DELAY_SEC)

    return sorted(
        companies_by_symbol.values(),
        key=lambda company: company.market_cap or Decimal("0"),
        reverse=True,
    )
