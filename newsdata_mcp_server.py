from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP


load_dotenv(encoding="utf-8-sig")
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP("jarvis-newsdata")

VALID_CATEGORIES = {
    "business",
    "crime",
    "domestic",
    "education",
    "entertainment",
    "environment",
    "food",
    "health",
    "lifestyle",
    "other",
    "politics",
    "science",
    "sports",
    "technology",
    "top",
    "tourism",
    "world",
}


def clean_query(value: str | None) -> str:
    query = (value or os.getenv("NEWSDATA_QUERY", "stock market")).strip()
    query = re.sub(r"\s+", " ", query)
    return query[:512] or "stock market"


def clean_code_list(value: str | None, default: str, code_length: int = 2) -> str:
    raw = (value or default).strip().lower()
    codes = []
    for part in raw.split(","):
        code = re.sub(r"[^a-z]", "", part)
        if len(code) == code_length:
            codes.append(code)
    return ",".join(codes[:5]) or default


def clean_category(value: str | None) -> str:
    raw = (value or os.getenv("NEWSDATA_CATEGORY", "business")).strip().lower()
    categories = []
    for part in raw.split(","):
        category = re.sub(r"[^a-z]", "", part)
        if category in VALID_CATEGORIES:
            categories.append(category)
    return ",".join(categories[:5]) or "business"


def clean_size(value: int | str | None) -> int:
    try:
        number = int(value or 10)
    except (TypeError, ValueError):
        number = 10
    return max(1, min(number, 50))


def article_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title"),
        "source": item.get("source_name") or item.get("source_id"),
        "link": item.get("link"),
        "published_at": item.get("pubDate"),
        "country": item.get("country"),
        "category": item.get("category"),
        "creator": item.get("creator"),
        "description": item.get("description"),
        "tickers": item.get("ai_tag") or item.get("keywords") or [],
    }


def fetch_latest_news(
    q: str | None = None,
    language: str | None = None,
    country: str | None = None,
    category: str | None = None,
    size: int | str | None = 10,
) -> dict[str, Any]:
    api_key = os.getenv("NEWSDATA_API_KEY", "").strip()
    if not api_key:
        return {
            "configured": False,
            "status": "missing_api_key",
            "results": [],
            "message": "Set NEWSDATA_API_KEY in .env.",
        }

    limit = clean_size(size)
    params: dict[str, Any] = {
        "apikey": api_key,
        "q": clean_query(q),
        "language": clean_code_list(language, os.getenv("NEWSDATA_LANGUAGE", "en")),
        "country": clean_code_list(country, os.getenv("NEWSDATA_COUNTRY", "us")),
        "category": clean_category(category),
        "size": limit,
    }

    base_url = os.getenv("NEWSDATA_BASE_URL", "https://newsdata.io/api/1").rstrip("/")
    try:
        with httpx.Client(timeout=20) as client:
            response = client.get(f"{base_url}/latest", params=params)
    except httpx.HTTPError as exc:
        return {
            "configured": True,
            "status": "request_error",
            "results": [],
            "message": f"{type(exc).__name__}: {exc}",
        }

    if response.is_error:
        return {
            "configured": True,
            "status": "error",
            "results": [],
            "message": f"Newsdata.io returned HTTP {response.status_code}: {response.text[:240]}",
            "sent_params": {key: value for key, value in params.items() if key != "apikey"},
        }

    data = response.json()
    return {
        "configured": True,
        "status": data.get("status", "success"),
        "query": {key: value for key, value in params.items() if key != "apikey"},
        "next_page": data.get("nextPage"),
        "results": [article_view(item) for item in data.get("results", [])[:limit]],
    }


@mcp.tool()
def get_latest_news(
    q: str = "",
    language: str = "",
    country: str = "",
    category: str = "",
    size: int = 10,
) -> str:
    """Fetch latest Newsdata.io headlines with sanitized query, country, language, category, and size parameters."""
    return json.dumps(fetch_latest_news(q, language, country, category, size), indent=2)


@mcp.tool()
def get_market_news(size: int = 10) -> str:
    """Fetch recent business-market headlines from Newsdata.io."""
    return json.dumps(fetch_latest_news("stock market earnings economy", "en", "us", "business", size), indent=2)


@mcp.tool()
def get_crypto_news(size: int = 10) -> str:
    """Fetch recent cryptocurrency-related headlines from Newsdata.io."""
    return json.dumps(fetch_latest_news("crypto bitcoin ethereum", "en", "us", "business", size), indent=2)


if __name__ == "__main__":
    mcp.run()
