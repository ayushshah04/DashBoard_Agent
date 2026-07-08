from __future__ import annotations

import os
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv
from fastapi import Body, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agent import OpenAIMCPAgent, DEFAULT_MODEL
from research_vault import add_note, delete_note, search_notes, summarize_ticker
from website_scan import scan_company_website


load_dotenv(encoding="utf-8-sig")

app = FastAPI(title="Jarvis Market Console")
app.mount("/static", StaticFiles(directory="static"), name="static")


def user_facing_error(exc: Exception) -> str:
    raw = str(exc)
    if "insufficient_quota" in raw or "exceeded your current quota" in raw:
        return (
            "OpenAI rejected the request because this API key has no usable quota. "
            "Add billing/credits or raise the project budget in the OpenAI dashboard, "
            "or replace OPENAI_API_KEY in .env with a key from a project that has quota. "
            "Then restart this server on port 8000."
        )
    if "invalid_api_key" in raw or "Incorrect API key" in raw:
        return "OpenAI rejected the API key. Replace OPENAI_API_KEY in .env, then restart this server."
    if "model_not_found" in raw:
        return "OpenAI rejected the selected model. Change OPENAI_MODEL in .env to a model enabled for your account."
    return f"{type(exc).__name__}: {exc}"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def split_env_urls(value: str) -> list[str]:
    normalized = value.replace("\n", ",").replace(";", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def embed_video_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    video_id = ""

    if host.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0]
    elif "youtube.com" in host:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith("/embed/"):
            video_id = parsed.path.split("/embed/", 1)[1].split("/")[0]
        elif parsed.path.startswith("/live/"):
            video_id = parsed.path.split("/live/", 1)[1].split("/")[0]

    if video_id:
        return f"https://www.youtube.com/embed/{video_id}?rel=0&modestbranding=1"
    return url


def news_video_feeds() -> list[dict[str, str]]:
    raw_urls = os.getenv("NEWS_VIDEO_URLS", "") or os.getenv("NEWS_VIDEO_URL", "")
    feeds = []
    for index, url in enumerate(split_env_urls(raw_urls), start=1):
        feeds.append(
            {
                "label": f"News video {index}",
                "source_url": url,
                "embed_url": embed_video_url(url),
            }
        )
    return feeds


def alpaca_headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", "").strip(),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "").strip(),
    }


def account_currency_fallback() -> str:
    return os.getenv("ACCOUNT_CURRENCY", "USD").strip().upper() or "USD"


RISK_LIMITS: dict[str, dict[str, object]] = {
    "max_order_notional_usd": {
        "env": "MAX_ORDER_NOTIONAL_USD",
        "default": "50",
        "minimum": 1,
        "maximum": 1_000_000,
        "integer": False,
    },
    "max_position_usd": {
        "env": "MAX_POSITION_USD",
        "default": "100",
        "minimum": 1,
        "maximum": 5_000_000,
        "integer": False,
    },
    "max_daily_loss_usd": {
        "env": "MAX_DAILY_LOSS_USD",
        "default": "250",
        "minimum": 1,
        "maximum": 1_000_000,
        "integer": False,
    },
    "options_max_contracts": {
        "env": "OPTIONS_MAX_CONTRACTS",
        "default": "1",
        "minimum": 0,
        "maximum": 100,
        "integer": True,
    },
}


def risk_settings_path() -> Path:
    configured = os.getenv("RISK_SETTINGS_PATH", "risk_settings.json")
    path = Path(configured)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def format_risk_value(value: float, integer: bool = False) -> str:
    if integer:
        return str(int(round(value)))
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def env_risk_settings() -> dict[str, str]:
    values: dict[str, str] = {}
    for key, meta in RISK_LIMITS.items():
        env_name = str(meta["env"])
        values[key] = os.getenv(env_name, str(meta["default"]))
    return values


def read_risk_overrides() -> dict[str, object]:
    path = risk_settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def normalize_risk_settings(payload: dict[str, object], base: dict[str, str] | None = None) -> dict[str, str]:
    base_values = base or env_risk_settings()
    normalized: dict[str, str] = {}
    for key, meta in RISK_LIMITS.items():
        raw_value = payload.get(key, base_values.get(key, str(meta["default"])))
        if raw_value is None or raw_value == "":
            raw_value = base_values.get(key, str(meta["default"]))
        try:
            number = float(str(raw_value).replace(",", ""))
        except ValueError as exc:
            raise ValueError(f"{key} must be a number.") from exc
        minimum = float(meta["minimum"])
        maximum = float(meta["maximum"])
        number = max(minimum, min(maximum, number))
        normalized[key] = format_risk_value(number, bool(meta["integer"]))
    return normalized


def current_risk_settings() -> dict[str, str]:
    return normalize_risk_settings(read_risk_overrides(), env_risk_settings())


def save_risk_settings(payload: dict[str, object]) -> dict[str, str]:
    settings = normalize_risk_settings(payload, current_risk_settings())
    path = risk_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings


POSITIVE_NEWS_TERMS = {
    "beat": 2,
    "beats": 2,
    "upgrade": 2,
    "upgraded": 2,
    "raises": 2,
    "raised": 2,
    "surge": 2,
    "surges": 2,
    "jump": 2,
    "jumps": 2,
    "rally": 2,
    "rallies": 2,
    "gain": 1,
    "gains": 1,
    "profit": 1,
    "profits": 1,
    "growth": 1,
    "record": 1,
    "strong": 1,
    "bullish": 2,
    "buy": 1,
    "outperform": 2,
    "breakthrough": 2,
    "approval": 2,
    "approved": 2,
    "deal": 1,
    "merger": 1,
    "acquire": 1,
    "dividend": 1,
    "buyback": 2,
    "guidance raised": 3,
    "price target raised": 3,
}


NEGATIVE_NEWS_TERMS = {
    "miss": 2,
    "misses": 2,
    "downgrade": 2,
    "downgraded": 2,
    "cuts": 2,
    "cut": 1,
    "falls": 2,
    "fall": 2,
    "drops": 2,
    "drop": 2,
    "plunge": 3,
    "plunges": 3,
    "slump": 2,
    "slumps": 2,
    "loss": 2,
    "losses": 2,
    "weak": 1,
    "bearish": 2,
    "sell": 1,
    "underperform": 2,
    "lawsuit": 2,
    "probe": 2,
    "investigation": 2,
    "recall": 2,
    "delay": 1,
    "delayed": 1,
    "risk": 1,
    "warning": 2,
    "bankruptcy": 3,
    "fraud": 3,
    "guidance cut": 3,
    "price target cut": 3,
}


def analyze_news_sentiment(*parts: object) -> dict[str, object]:
    text = " ".join(str(part or "") for part in parts).lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))

    positive_hits: list[str] = []
    negative_hits: list[str] = []
    score = 0

    for term, weight in POSITIVE_NEWS_TERMS.items():
        matched = term in text if " " in term else term in tokens
        if matched:
            positive_hits.append(term)
            score += weight
    for term, weight in NEGATIVE_NEWS_TERMS.items():
        matched = term in text if " " in term else term in tokens
        if matched:
            negative_hits.append(term)
            score -= weight

    if score >= 2:
        label = "positive"
    elif score <= -2:
        label = "negative"
    else:
        label = "neutral"

    reason_parts = []
    if positive_hits:
        reason_parts.append("Positive: " + ", ".join(positive_hits[:4]))
    if negative_hits:
        reason_parts.append("Negative: " + ", ".join(negative_hits[:4]))
    reason = "; ".join(reason_parts) if reason_parts else "No strong directional keyword signal."

    confidence = 0.5 if score == 0 else min(0.95, 0.5 + min(abs(score), 6) * 0.075)
    return {
        "sentiment_label": label,
        "sentiment_score": score,
        "sentiment_confidence": round(confidence, 2),
        "sentiment_reason": reason,
    }


MARKET_GROUPS: dict[str, list[dict[str, str]]] = {
    "PRE-MKT": [
        {"label": "S&P 500", "symbol": "SPY", "kind": "stock", "href": "https://www.cnbc.com/quotes/.SPX"},
        {"label": "NASDAQ 100", "symbol": "QQQ", "kind": "stock", "href": "https://www.cnbc.com/quotes/.NDX"},
        {"label": "DJIA", "symbol": "DIA", "kind": "stock", "href": "https://www.cnbc.com/quotes/.DJI"},
        {"label": "RUSS 2K", "symbol": "IWM", "kind": "stock", "href": "https://www.cnbc.com/quotes/.RUT"},
    ],
    "ASIA": [
        {"label": "Japan", "symbol": "EWJ", "kind": "stock", "href": "https://www.cnbc.com/quotes/EWJ"},
        {"label": "China Large", "symbol": "FXI", "kind": "stock", "href": "https://www.cnbc.com/quotes/FXI"},
        {"label": "India", "symbol": "INDA", "kind": "stock", "href": "https://www.cnbc.com/quotes/INDA"},
        {"label": "Taiwan", "symbol": "EWT", "kind": "stock", "href": "https://www.cnbc.com/quotes/EWT"},
    ],
    "EUR": [
        {"label": "Eurozone", "symbol": "FEZ", "kind": "stock", "href": "https://www.cnbc.com/quotes/FEZ"},
        {"label": "UK", "symbol": "EWU", "kind": "stock", "href": "https://www.cnbc.com/quotes/EWU"},
        {"label": "Germany", "symbol": "EWG", "kind": "stock", "href": "https://www.cnbc.com/quotes/EWG"},
        {"label": "France", "symbol": "EWQ", "kind": "stock", "href": "https://www.cnbc.com/quotes/EWQ"},
    ],
    "BONDS": [
        {"label": "20Y Treasury", "symbol": "TLT", "kind": "stock", "href": "https://www.cnbc.com/quotes/TLT"},
        {"label": "7-10Y Treasury", "symbol": "IEF", "kind": "stock", "href": "https://www.cnbc.com/quotes/IEF"},
        {"label": "1-3Y Treasury", "symbol": "SHY", "kind": "stock", "href": "https://www.cnbc.com/quotes/SHY"},
        {"label": "High Yield", "symbol": "HYG", "kind": "stock", "href": "https://www.cnbc.com/quotes/HYG"},
    ],
    "OIL": [
        {"label": "US Oil", "symbol": "USO", "kind": "stock", "href": "https://www.cnbc.com/quotes/USO"},
        {"label": "Brent Oil", "symbol": "BNO", "kind": "stock", "href": "https://www.cnbc.com/quotes/BNO"},
        {"label": "Energy", "symbol": "XLE", "kind": "stock", "href": "https://www.cnbc.com/quotes/XLE"},
        {"label": "Oil Services", "symbol": "OIH", "kind": "stock", "href": "https://www.cnbc.com/quotes/OIH"},
    ],
    "GOLD": [
        {"label": "Gold", "symbol": "GLD", "kind": "stock", "href": "https://www.cnbc.com/quotes/GLD"},
        {"label": "Silver", "symbol": "SLV", "kind": "stock", "href": "https://www.cnbc.com/quotes/SLV"},
        {"label": "Gold Miners", "symbol": "GDX", "kind": "stock", "href": "https://www.cnbc.com/quotes/GDX"},
        {"label": "Junior Miners", "symbol": "GDXJ", "kind": "stock", "href": "https://www.cnbc.com/quotes/GDXJ"},
    ],
    "FX": [
        {"label": "US Dollar", "symbol": "UUP", "kind": "stock", "href": "https://www.cnbc.com/quotes/UUP"},
        {"label": "Euro", "symbol": "FXE", "kind": "stock", "href": "https://www.cnbc.com/quotes/FXE"},
        {"label": "Yen", "symbol": "FXY", "kind": "stock", "href": "https://www.cnbc.com/quotes/FXY"},
        {"label": "Pound", "symbol": "FXB", "kind": "stock", "href": "https://www.cnbc.com/quotes/FXB"},
    ],
    "CRYPTO": [
        {"label": "Bitcoin", "symbol": "BTC/USD", "kind": "crypto", "href": "https://www.cnbc.com/quotes/BTC.CM="},
        {"label": "Ethereum", "symbol": "ETH/USD", "kind": "crypto", "href": "https://www.cnbc.com/quotes/ETH.CM="},
        {"label": "Solana", "symbol": "SOL/USD", "kind": "crypto", "href": "https://www.cnbc.com/quotes/SOL.CM="},
        {"label": "Dogecoin", "symbol": "DOGE/USD", "kind": "crypto", "href": "https://www.cnbc.com/quotes/DOGE.CM="},
    ],
    "US": [
        {"label": "DJIA", "symbol": "DIA", "kind": "stock", "href": "https://www.cnbc.com/quotes/.DJI"},
        {"label": "S&P 500", "symbol": "SPY", "kind": "stock", "href": "https://www.cnbc.com/quotes/.SPX"},
        {"label": "NASDAQ 100", "symbol": "QQQ", "kind": "stock", "href": "https://www.cnbc.com/quotes/.NDX"},
        {"label": "RUSS 2K", "symbol": "IWM", "kind": "stock", "href": "https://www.cnbc.com/quotes/.RUT"},
    ],
}


MOVER_WATCHLISTS: dict[str, list[str]] = {
    "S&P": ["NVDA", "MSFT", "AAPL", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "JPM", "XOM", "LLY", "WMT"],
    "NASDAQ": ["NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "META", "AMD", "QCOM", "INTC", "NFLX", "ADBE", "CSCO"],
    "DOW": ["UNH", "GS", "MSFT", "AAPL", "AMGN", "CAT", "JPM", "MCD", "HD", "CRM", "V", "BA"],
    "EUR": ["FEZ", "VGK", "EWU", "EWG", "EWQ", "EWI", "EWP", "EWL"],
    "ASIA": ["EWJ", "FXI", "INDA", "EWT", "EWY", "EWH", "EWA", "MCHI"],
}


CRYPTO_SYMBOL_ALIASES = {
    "BTC": "BTC/USD",
    "ETH": "ETH/USD",
    "SOL": "SOL/USD",
    "DOGE": "DOGE/USD",
}


def unique_market_symbols(kind: str) -> list[str]:
    seen: set[str] = set()
    symbols: list[str] = []
    for items in MARKET_GROUPS.values():
        for item in items:
            if item["kind"] == kind and item["symbol"] not in seen:
                seen.add(item["symbol"])
                symbols.append(item["symbol"])
    return symbols


def snapshot_value(snapshot: dict[str, object], *path: str) -> object:
    current: object = snapshot
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def market_item_payload(item: dict[str, str], snapshots: dict[str, dict[str, object]]) -> dict[str, object]:
    snapshot = snapshots.get(item["symbol"], {})
    last = (
        to_float(snapshot_value(snapshot, "latestTrade", "p"))
        or to_float(snapshot_value(snapshot, "dailyBar", "c"))
        or to_float(snapshot_value(snapshot, "minuteBar", "c"))
    )
    previous = to_float(snapshot_value(snapshot, "prevDailyBar", "c"))
    change = last - previous if last is not None and previous else None
    change_percent = (change / previous * 100) if change is not None and previous else None
    if change is None:
        trend = "No trend"
    elif change > 0:
        trend = "Uptrend"
    elif change < 0:
        trend = "Downtrend"
    else:
        trend = "Flat"
    timestamp = (
        snapshot_value(snapshot, "latestTrade", "t")
        or snapshot_value(snapshot, "latestQuote", "t")
        or snapshot_value(snapshot, "minuteBar", "t")
        or snapshot_value(snapshot, "dailyBar", "t")
    )
    return {
        "label": item["label"],
        "symbol": item["symbol"],
        "kind": item["kind"],
        "href": item["href"],
        "price": last,
        "trend": trend,
        "change": change,
        "change_percent": change_percent,
        "timestamp": timestamp,
    }


def stock_snapshot_payload(
    symbol: str,
    snapshot: dict[str, object],
    name: str | None = None,
    volume: float | int | None = None,
    trade_count: float | int | None = None,
    volume_ratio: float | None = None,
) -> dict[str, object]:
    last = (
        to_float(snapshot_value(snapshot, "latestTrade", "p"))
        or to_float(snapshot_value(snapshot, "dailyBar", "c"))
        or to_float(snapshot_value(snapshot, "minuteBar", "c"))
    )
    previous = to_float(snapshot_value(snapshot, "prevDailyBar", "c"))
    change = last - previous if last is not None and previous else None
    change_percent = (change / previous * 100) if change is not None and previous else None
    timestamp = (
        snapshot_value(snapshot, "latestTrade", "t")
        or snapshot_value(snapshot, "latestQuote", "t")
        or snapshot_value(snapshot, "minuteBar", "t")
        or snapshot_value(snapshot, "dailyBar", "t")
    )
    return {
        "symbol": symbol,
        "name": name or symbol,
        "href": f"https://www.cnbc.com/quotes/{symbol}",
        "price": last,
        "change": change,
        "change_percent": change_percent,
        "volume": volume if volume is not None else snapshot_value(snapshot, "dailyBar", "v"),
        "trade_count": trade_count,
        "volume_ratio": volume_ratio,
        "timestamp": timestamp,
    }


def symbol_list(value: str | None, default: str = "") -> list[str]:
    raw = value or default
    seen: set[str] = set()
    symbols: list[str] = []
    for item in raw.replace("\n", ",").replace(";", ",").split(","):
        symbol = item.strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def watch_symbol_groups(value: str | None) -> tuple[list[str], list[str]]:
    stocks: list[str] = []
    crypto: list[str] = []
    for symbol in symbol_list(value, os.getenv("WATCHLIST_SYMBOLS", "TSLA,NVDA,SPY")):
        pair = CRYPTO_SYMBOL_ALIASES.get(symbol, symbol if "/" in symbol else "")
        if pair:
            crypto.append(pair)
        else:
            stocks.append(symbol)
    return stocks, crypto


async def fetch_stock_snapshots(
    client: httpx.AsyncClient,
    data_base_url: str,
    symbols: list[str],
    warnings: list[str],
) -> dict[str, dict[str, object]]:
    if not symbols:
        return {}
    response = await client.get(
        f"{data_base_url}/v2/stocks/snapshots",
        params={"symbols": ",".join(symbols)},
    )
    if response.is_error:
        warnings.append(f"Alpaca stock snapshots returned HTTP {response.status_code}.")
        return {}
    return response.json()


async def fetch_asset_names(
    client: httpx.AsyncClient,
    trading_base_url: str,
    symbols: list[str],
) -> dict[str, str]:
    names: dict[str, str] = {}
    semaphore = asyncio.Semaphore(8)

    async def fetch_one(symbol: str) -> None:
        async with semaphore:
            try:
                response = await client.get(f"{trading_base_url}/assets/{symbol}")
            except httpx.HTTPError:
                return
            if not response.is_error:
                data = response.json()
                names[symbol] = data.get("name") or symbol

    await asyncio.gather(*(fetch_one(symbol) for symbol in symbols))
    return names


async def fetch_assets_details(
    client: httpx.AsyncClient,
    trading_base_url: str,
    symbols: list[str],
) -> dict[str, dict[str, object]]:
    assets: dict[str, dict[str, object]] = {}
    semaphore = asyncio.Semaphore(8)

    async def fetch_one(symbol: str) -> None:
        async with semaphore:
            try:
                response = await client.get(f"{trading_base_url}/assets/{symbol}")
            except httpx.HTTPError:
                return
            if not response.is_error:
                assets[symbol] = response.json()

    await asyncio.gather(*(fetch_one(symbol) for symbol in symbols))
    return assets


async def fetch_screener_movers(
    client: httpx.AsyncClient,
    data_base_url: str,
    top: int,
    warnings: list[str],
) -> dict[str, object]:
    response = await client.get(f"{data_base_url}/v1beta1/screener/stocks/movers", params={"top": top})
    if response.is_error:
        warnings.append(f"Alpaca market movers returned HTTP {response.status_code}.")
        return {"gainers": [], "losers": [], "last_updated": None}
    return response.json()


async def fetch_most_actives(
    client: httpx.AsyncClient,
    data_base_url: str,
    by: str,
    top: int,
    warnings: list[str],
) -> dict[str, object]:
    response = await client.get(
        f"{data_base_url}/v1beta1/screener/stocks/most-actives",
        params={"by": by, "top": top},
    )
    if response.is_error:
        warnings.append(f"Alpaca most-actives returned HTTP {response.status_code}.")
        return {"most_actives": [], "last_updated": None}
    return response.json()


async def fetch_daily_bars(
    client: httpx.AsyncClient,
    data_base_url: str,
    symbols: list[str],
    warnings: list[str],
) -> dict[str, list[dict[str, object]]]:
    if not symbols:
        return {}
    start = (datetime.now(timezone.utc) - timedelta(days=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    response = await client.get(
        f"{data_base_url}/v2/stocks/bars",
        params={"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "limit": 1000},
    )
    if response.is_error:
        warnings.append(f"Alpaca daily bars returned HTTP {response.status_code}.")
        return {}
    return response.json().get("bars", {})


def ten_day_volume_ratio(current_volume: object, bars: list[dict[str, object]]) -> float | None:
    current = to_float(current_volume)
    volumes = [to_float(bar.get("v")) for bar in bars]
    volumes = [volume for volume in volumes if volume and volume > 0]
    if current is None or not volumes:
        return None
    prior = volumes[-11:-1] if len(volumes) > 10 else volumes[:-1]
    if not prior:
        prior = volumes[-10:]
    average = sum(prior) / len(prior)
    return current / average if average else None


def trade_side_from_candidate(change_percent: float | None, asset: dict[str, object], kind: str) -> tuple[str, str]:
    change = change_percent or 0
    if kind == "crypto":
        return "long", "Crypto Scout uses long-only paper candidates."
    if change < -1.5 and asset.get("shortable") and asset.get("easy_to_borrow"):
        return "short-watch", "Shortable and easy-to-borrow loser with downside momentum."
    if change < -1.5:
        return "skip-short", "Downside momentum detected, but shortability is not confirmed."
    return "long", "Momentum candidate from Alpaca mover/snapshot data."


def scout_prices(price: float | None, direction: str, kind: str) -> dict[str, object]:
    if not price or price <= 0:
        return {"entry": "--", "stop": "--", "exit_target": "--"}
    def fmt(value: float) -> str:
        digits = 4 if value < 1 else 2
        return f"${value:.{digits}f}"

    risk_pct = 0.015 if kind == "crypto" else 0.01
    reward_pct = 0.03 if kind == "crypto" else 0.02
    if direction.startswith("short"):
        stop = price * (1 + risk_pct)
        target = price * (1 - reward_pct)
    else:
        stop = price * (1 - risk_pct)
        target = price * (1 + reward_pct)
    return {
        "entry": fmt(price),
        "stop": fmt(stop),
        "exit_target": fmt(target),
    }


def scout_score(
    change_percent: float | None,
    volume_ratio: float | None,
    news_count: int,
    direction: str,
    tradable: bool,
) -> int:
    score = 0
    change = abs(change_percent or 0)
    score += min(35, int(change * 5))
    if volume_ratio:
        score += min(25, int(volume_ratio * 6))
    score += min(15, news_count * 5)
    if direction in {"long", "short-watch"}:
        score += 15
    if tradable:
        score += 10
    return max(0, min(score, 100))


def scout_candidate_payload(
    *,
    symbol: str,
    kind: str,
    snapshot: dict[str, object],
    asset: dict[str, object] | None = None,
    source: str,
    news_count: int = 0,
    volume_ratio: float | None = None,
) -> dict[str, object] | None:
    asset = asset or {}
    price = (
        to_float(snapshot_value(snapshot, "latestTrade", "p"))
        or to_float(snapshot_value(snapshot, "dailyBar", "c"))
        or to_float(snapshot_value(snapshot, "minuteBar", "c"))
    )
    previous = to_float(snapshot_value(snapshot, "prevDailyBar", "c"))
    if not price:
        return None
    change = price - previous if previous else None
    change_percent = (change / previous * 100) if change is not None and previous else None
    direction, reason = trade_side_from_candidate(change_percent, asset, kind)
    tradable = kind == "crypto" or bool(asset.get("tradable", True))
    prices = scout_prices(price, direction, kind)
    score = scout_score(change_percent, volume_ratio, news_count, direction, tradable)
    if direction == "skip-short":
        status = "Skipped"
    elif score >= 55:
        status = "Ticket ready"
    else:
        status = "Watch"
    if not tradable:
        status = "Blocked"
        reason = "Asset is not marked tradable by Alpaca."

    return {
        "symbol": symbol,
        "asset_class": "crypto" if kind == "crypto" else "equity",
        "direction": direction,
        "status": status,
        "outcome": "skipped" if status in {"Skipped", "Blocked"} else "open",
        "score": score,
        "price": price,
        "change": change,
        "change_percent": change_percent,
        "volume": snapshot_value(snapshot, "dailyBar", "v"),
        "volume_ratio": volume_ratio,
        "news_count": news_count,
        "shortable": bool(asset.get("shortable", False)),
        "easy_to_borrow": bool(asset.get("easy_to_borrow", False)),
        "tradable": tradable,
        "entry": prices["entry"],
        "exit_target": prices["exit_target"],
        "stop": prices["stop"],
        "size": "",
        "source": source,
        "reason": reason,
    }


def trading_status() -> dict[str, object]:
    paper = env_bool("ALPACA_PAPER_TRADE", True)
    live_unlocked = (
        not paper
        and env_bool("LIVE_TRADING_ENABLED", False)
        and os.getenv("LIVE_TRADING_CONFIRMATION") == "I_UNDERSTAND_LIVE_TRADING_RISK"
    )
    video_feeds = news_video_feeds()
    risk_settings = current_risk_settings()
    return {
        "paper": paper,
        "mode": "paper" if paper else "live",
        "live_trading_enabled": live_unlocked,
        "order_tools_locked": not paper and not live_unlocked,
        "base_url": os.getenv("ALPACA_BASE_URL", ""),
        "currency": account_currency_fallback(),
        "robinhood": {
            "mcp_url": os.getenv("ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading"),
            "mcp_enabled": env_bool("ROBINHOOD_MCP_ENABLED", False),
            "trading_enabled": (
                env_bool("ROBINHOOD_TRADING_ENABLED", False)
                and os.getenv("ROBINHOOD_TRADING_CONFIRMATION") == "I_UNDERSTAND_ROBINHOOD_AGENTIC_TRADING_RISK"
            ),
            "auth_note": "Authenticate the Robinhood Trading MCP on desktop and use a dedicated Agentic account.",
        },
        "toolsets": [item.strip() for item in os.getenv("ALPACA_TOOLSETS", "").split(",") if item.strip()],
        "markets": [item.strip() for item in os.getenv("MARKET_UNIVERSE", "equities,crypto,currency").split(",") if item.strip()],
        "risk": risk_settings,
        "risk_source": "dashboard" if read_risk_overrides() else "environment",
        "news": {
            "video_url": video_feeds[0]["embed_url"] if video_feeds else "",
            "video_urls": video_feeds,
            "factors": [
                "breaking headlines",
                "earnings and guidance",
                "market movers",
                "macro calendar",
                "crypto catalysts",
                "corporate actions",
            ],
        },
        "research": {
            "company_website_scan": True,
            "long_term_horizon": os.getenv("LONG_TERM_HORIZON", "3-5 years"),
            "options_enabled": "options-data" in os.getenv("ALPACA_TOOLSETS", ""),
        },
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "model": os.getenv("OPENAI_MODEL", DEFAULT_MODEL)}


@app.get("/api/trading/config")
async def trading_config() -> dict[str, object]:
    return trading_status()


@app.post("/api/risk/settings")
async def update_risk_settings(payload: dict[str, object] = Body(...)) -> dict[str, object]:
    try:
        settings = await asyncio.to_thread(save_risk_settings, payload)
    except ValueError as exc:
        return {"status": "error", "message": str(exc), "risk": current_risk_settings()}
    return {"status": "saved", "risk": settings, "risk_source": "dashboard"}


@app.get("/api/trading/prompts")
async def trading_prompts() -> dict[str, str]:
    status = trading_status()
    risk = status["risk"]
    markets = ", ".join(status["markets"])
    return {
        "paper_research": (
            f"Screen {markets} using Alpaca market movers, snapshots, account exposure, and news. "
            "Rank candidates by liquidity, trend, volatility, and news risk. Do not place orders."
        ),
        "paper_trade": (
            f"Using paper trading only, screen {markets}, apply max order notional "
            f"${risk['max_order_notional_usd']} and max position ${risk['max_position_usd']}, "
            "then place only one small paper trade if the setup is strong. Explain every tool call."
        ),
        "trade_ticket": (
            f"Screen {markets}, use account exposure, market movers, snapshots, Alpaca news, Newsdata.io, "
            f"Research Vault context, and risk limits. Return one execution-ready trade ticket only: symbol, "
            f"asset class, direction, order type, notional/quantity, entry, stop/invalidation, profit/risk target, "
            f"news risk, and EXECUTE/SKIP decision. Do not place orders. End with a TRADE_RECORD block containing "
            f"status, symbol, asset_class, direction, entry, exit_target, stop, quantity_or_notional, order_id, and reason."
        ),
        "continuous_paper_scout": (
            f"Use /api/alpaca/scout as the primary Scout engine across {markets}. It is Alpaca-first, does not call "
            f"OpenAI, and returns ranked candidates using movers, snapshots, assets, account exposure, news counts, "
            f"and risk limits. Only use the agent after a user asks for deeper reasoning, a trade ticket, or paper execution."
        ),
        "live_guarded": (
            f"If and only if live trading is unlocked by the backend, screen {markets}, check news and account risk, "
            f"respect max order notional ${risk['max_order_notional_usd']}, max position ${risk['max_position_usd']}, "
            f"and max daily loss ${risk['max_daily_loss_usd']}. Prepare the order plan first; execute only if I explicitly confirm."
        ),
        "long_term_investment": (
            "Build a long-term investment research report for TICKER over a 3-5 year horizon. "
            "Use Alpaca market data, Newsdata.io headlines, company website scan if I provide a website URL, "
            "and available account exposure. Cover business quality, moat, growth drivers, balance-sheet risks, "
            "valuation context, catalysts, downside risks, and a watch/buy/avoid recommendation. Do not place orders."
        ),
        "company_website_due_diligence": (
            "Scan this company website URL: https://example.com. Extract investor relations, products, pricing, "
            "customers, careers, news, security, and leadership signals. Then connect those signals to a long-term "
            "investment thesis. Do not place orders."
        ),
        "options_analysis": (
            "Analyze options for TICKER using options-data only. Review expirations, liquidity, spread quality, "
            "basic strategy fit, assignment risk, and max loss. Suggest only education/research candidates. "
            "Do not place option orders unless I explicitly ask and the backend allows order tools."
        ),
    }


@app.get("/api/mcp/registry")
async def mcp_registry(
    config_path: str = Query(default="mcp_config.json"),
    model: str | None = Query(default=None),
) -> dict[str, object]:
    async with OpenAIMCPAgent(model=model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL), config_path=config_path) as agent:
        return {
            "model": agent.model,
            "servers": list(agent.servers),
            "tools": [tool["name"] for tool in agent.openai_tools],
            "warnings": agent.connection_warnings,
        }


@app.get("/api/alpaca/account")
async def alpaca_account() -> dict[str, object]:
    headers = alpaca_headers()
    if not headers["APCA-API-KEY-ID"] or not headers["APCA-API-SECRET-KEY"]:
        return {
            "configured": False,
            "message": "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env to show account funds.",
        }

    base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2").rstrip("/")
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        response = await client.get(f"{base_url}/account")
        if response.is_error:
            return {
                "configured": True,
                "status": "error",
                "message": f"Alpaca returned HTTP {response.status_code}: {response.text[:240]}",
            }
        account = response.json()

    keys = [
        "account_number",
        "status",
        "currency",
        "cash",
        "buying_power",
        "regt_buying_power",
        "daytrading_buying_power",
        "non_marginable_buying_power",
        "portfolio_value",
        "equity",
        "last_equity",
        "long_market_value",
        "short_market_value",
        "multiplier",
        "daytrade_count",
        "pattern_day_trader",
        "trading_blocked",
        "account_blocked",
        "transfers_blocked",
        "trade_suspended_by_user",
    ]
    return {
        "configured": True,
        "mode": "paper" if env_bool("ALPACA_PAPER_TRADE", True) else "live",
        "currency": account.get("currency") or account_currency_fallback(),
        "account": {key: account.get(key) for key in keys if key in account},
    }


@app.get("/api/markets/overview")
async def markets_overview() -> dict[str, object]:
    headers = alpaca_headers()
    groups: dict[str, list[dict[str, object]]] = {name: [] for name in MARKET_GROUPS}
    if not headers["APCA-API-KEY-ID"] or not headers["APCA-API-SECRET-KEY"]:
        return {
            "configured": False,
            "groups": groups,
            "message": "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env to load market snapshots.",
        }

    data_base_url = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")
    stock_snapshots: dict[str, dict[str, object]] = {}
    crypto_snapshots: dict[str, dict[str, object]] = {}
    warnings: list[str] = []

    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        stock_symbols = unique_market_symbols("stock")
        if stock_symbols:
            stock_response = await client.get(
                f"{data_base_url}/v2/stocks/snapshots",
                params={"symbols": ",".join(stock_symbols)},
            )
            if stock_response.is_error:
                warnings.append(f"Alpaca stock snapshots returned HTTP {stock_response.status_code}.")
            else:
                stock_snapshots = stock_response.json()

        crypto_symbols = unique_market_symbols("crypto")
        if crypto_symbols:
            crypto_response = await client.get(
                f"{data_base_url}/v1beta3/crypto/us/snapshots",
                params={"symbols": ",".join(crypto_symbols)},
            )
            if crypto_response.is_error:
                warnings.append(f"Alpaca crypto snapshots returned HTTP {crypto_response.status_code}.")
            else:
                crypto_snapshots = crypto_response.json().get("snapshots", {})

    for group_name, items in MARKET_GROUPS.items():
        for item in items:
            source = crypto_snapshots if item["kind"] == "crypto" else stock_snapshots
            groups[group_name].append(market_item_payload(item, source))

    return {
        "configured": True,
        "source": "Alpaca market snapshots",
        "groups": groups,
        "warnings": warnings,
    }


@app.get("/api/markets/movers")
async def markets_movers(top: int = Query(default=5, ge=3, le=10)) -> dict[str, object]:
    headers = alpaca_headers()
    if not headers["APCA-API-KEY-ID"] or not headers["APCA-API-SECRET-KEY"]:
        return {
            "configured": False,
            "message": "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env to load market movers.",
            "mover_tabs": {},
            "most_active": [],
            "unusual_volume": [],
        }

    data_base_url = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")
    trading_base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2").rstrip("/")
    warnings: list[str] = []

    async with httpx.AsyncClient(timeout=25, headers=headers) as client:
        screener, most_volume, most_trades = await asyncio.gather(
            fetch_screener_movers(client, data_base_url, top, warnings),
            fetch_most_actives(client, data_base_url, "volume", top, warnings),
            fetch_most_actives(client, data_base_url, "trades", max(top * 2, 10), warnings),
        )

        mover_symbols = [
            item.get("symbol")
            for item in [*screener.get("gainers", []), *screener.get("losers", [])]
            if item.get("symbol")
        ]
        active_symbols = [item.get("symbol") for item in most_volume.get("most_actives", []) if item.get("symbol")]
        trade_symbols = [item.get("symbol") for item in most_trades.get("most_actives", []) if item.get("symbol")]
        watch_symbols = [symbol for symbols in MOVER_WATCHLISTS.values() for symbol in symbols]
        all_symbols = symbol_list(",".join([*mover_symbols, *active_symbols, *trade_symbols, *watch_symbols]))

        snapshots, asset_names, bars = await asyncio.gather(
            fetch_stock_snapshots(client, data_base_url, all_symbols, warnings),
            fetch_asset_names(client, trading_base_url, all_symbols),
            fetch_daily_bars(client, data_base_url, symbol_list(",".join([*mover_symbols, *active_symbols, *trade_symbols])), warnings),
        )

    def with_names(items: list[dict[str, object]]) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for item in items[:top]:
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            payload = stock_snapshot_payload(
                symbol=symbol,
                snapshot=snapshots.get(symbol, {}),
                name=asset_names.get(symbol),
                volume=item.get("volume"),
                trade_count=item.get("trade_count"),
            )
            payload["price"] = payload["price"] if payload["price"] is not None else item.get("price")
            payload["change"] = payload["change"] if payload["change"] is not None else item.get("change")
            payload["change_percent"] = (
                payload["change_percent"] if payload["change_percent"] is not None else item.get("percent_change")
            )
            rows.append(payload)
        return rows

    mover_tabs: dict[str, dict[str, list[dict[str, object]]]] = {
        "US": {
            "top": with_names(screener.get("gainers", [])),
            "bottom": with_names(screener.get("losers", [])),
        }
    }

    for group_name, symbols in MOVER_WATCHLISTS.items():
        rows = [
            stock_snapshot_payload(symbol, snapshots.get(symbol, {}), asset_names.get(symbol))
            for symbol in symbols
            if snapshots.get(symbol)
        ]
        rows.sort(key=lambda item: to_float(item.get("change_percent")) or 0, reverse=True)
        mover_tabs[group_name] = {
            "top": rows[:top],
            "bottom": list(reversed(rows[-top:])),
        }

    most_active = with_names(most_volume.get("most_actives", []))

    unusual_pool = symbol_list(",".join([*active_symbols, *trade_symbols, *mover_symbols]))
    unusual_rows: list[dict[str, object]] = []
    volume_by_symbol = {
        str(item.get("symbol", "")).upper(): item.get("volume")
        for item in [*most_volume.get("most_actives", []), *most_trades.get("most_actives", [])]
        if item.get("symbol")
    }
    for symbol in unusual_pool:
        snapshot = snapshots.get(symbol, {})
        current_volume = volume_by_symbol.get(symbol) or snapshot_value(snapshot, "dailyBar", "v")
        ratio = ten_day_volume_ratio(current_volume, bars.get(symbol, []))
        if ratio is None:
            continue
        row = stock_snapshot_payload(
            symbol=symbol,
            snapshot=snapshot,
            name=asset_names.get(symbol),
            volume=current_volume,
            volume_ratio=ratio,
        )
        unusual_rows.append(row)
    unusual_rows.sort(key=lambda item: to_float(item.get("volume_ratio")) or 0, reverse=True)

    return {
        "configured": True,
        "source": "Alpaca screener, snapshots, assets, and daily bars",
        "last_updated": screener.get("last_updated") or most_volume.get("last_updated"),
        "mover_tabs": mover_tabs,
        "most_active": most_active,
        "unusual_volume": unusual_rows[:top],
        "warnings": warnings,
    }


@app.get("/api/alpaca/scout")
async def alpaca_scout(
    symbols: str | None = Query(default=None),
    top: int = Query(default=8, ge=3, le=20),
    include_crypto: bool = Query(default=True),
) -> dict[str, object]:
    headers = alpaca_headers()
    if not headers["APCA-API-KEY-ID"] or not headers["APCA-API-SECRET-KEY"]:
        return {
            "configured": False,
            "status": "missing_keys",
            "message": "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env to run Alpaca Scout.",
            "candidates": [],
            "warnings": [],
        }

    data_base_url = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")
    trading_base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2").rstrip("/")
    warnings: list[str] = []
    requested = symbol_list(symbols, os.getenv("WATCHLIST_SYMBOLS", "TSLA,NVDA,SPY"))
    crypto_requested = [CRYPTO_SYMBOL_ALIASES.get(symbol, symbol) for symbol in requested if CRYPTO_SYMBOL_ALIASES.get(symbol) or "/" in symbol]
    stock_requested = [
        symbol
        for symbol in requested
        if not CRYPTO_SYMBOL_ALIASES.get(symbol) and "/" not in symbol
    ]

    async with httpx.AsyncClient(timeout=25, headers=headers) as client:
        screener, most_volume, most_trades, account_response, positions_response = await asyncio.gather(
            fetch_screener_movers(client, data_base_url, max(top, 10), warnings),
            fetch_most_actives(client, data_base_url, "volume", max(top, 10), warnings),
            fetch_most_actives(client, data_base_url, "trades", max(top, 10), warnings),
            client.get(f"{trading_base_url}/account"),
            client.get(f"{trading_base_url}/positions"),
        )

        mover_symbols = [
            str(item.get("symbol", "")).upper()
            for item in [*screener.get("gainers", []), *screener.get("losers", [])]
            if item.get("symbol")
        ]
        active_symbols = [
            str(item.get("symbol", "")).upper()
            for item in [*most_volume.get("most_actives", []), *most_trades.get("most_actives", [])]
            if item.get("symbol")
        ]
        stock_symbols = symbol_list(",".join([*stock_requested, *mover_symbols, *active_symbols]))[:80]
        crypto_symbols = symbol_list(",".join([*crypto_requested, "BTC/USD", "ETH/USD", "SOL/USD"])) if include_crypto else []

        stocks_task = fetch_stock_snapshots(client, data_base_url, stock_symbols, warnings)
        assets_task = fetch_assets_details(client, trading_base_url, stock_symbols)
        bars_task = fetch_daily_bars(client, data_base_url, stock_symbols, warnings)
        news_task = client.get(
            f"{data_base_url}/v1beta1/news",
            params={"symbols": ",".join(stock_symbols[:50]), "limit": 50, "sort": "desc"} if stock_symbols else {"limit": 1},
        )
        crypto_task = client.get(
            f"{data_base_url}/v1beta3/crypto/us/snapshots",
            params={"symbols": ",".join(crypto_symbols)},
        ) if crypto_symbols else None

        if crypto_task:
            stock_snapshots, assets, bars, news_response, crypto_response = await asyncio.gather(
                stocks_task, assets_task, bars_task, news_task, crypto_task
            )
            if crypto_response.is_error:
                warnings.append(f"Alpaca crypto scout snapshots returned HTTP {crypto_response.status_code}.")
                crypto_snapshots: dict[str, dict[str, object]] = {}
            else:
                crypto_snapshots = crypto_response.json().get("snapshots", {})
        else:
            stock_snapshots, assets, bars, news_response = await asyncio.gather(
                stocks_task, assets_task, bars_task, news_task
            )
            crypto_snapshots = {}

    account = account_response.json() if not account_response.is_error else {}
    positions = positions_response.json() if not positions_response.is_error else []
    held_symbols = {
        str(position.get("symbol", "")).upper(): position
        for position in positions
        if isinstance(position, dict) and position.get("symbol")
    }
    buying_power = to_float(account.get("buying_power")) or 0
    risk_settings = current_risk_settings()
    max_order = to_float(risk_settings.get("max_order_notional_usd")) or 50
    paper_mode = env_bool("ALPACA_PAPER_TRADE", True)

    news_counts: dict[str, int] = {}
    if news_response.is_error:
        warnings.append(f"Alpaca scout news returned HTTP {news_response.status_code}.")
    else:
        for item in news_response.json().get("news", []):
            for symbol in item.get("symbols", []) or []:
                ticker = str(symbol).upper()
                news_counts[ticker] = news_counts.get(ticker, 0) + 1

    volume_by_symbol = {
        str(item.get("symbol", "")).upper(): item.get("volume")
        for item in [*most_volume.get("most_actives", []), *most_trades.get("most_actives", [])]
        if item.get("symbol")
    }
    candidate_map: dict[str, dict[str, object]] = {}
    for symbol in stock_symbols:
        snapshot = stock_snapshots.get(symbol, {})
        current_volume = volume_by_symbol.get(symbol) or snapshot_value(snapshot, "dailyBar", "v")
        ratio = ten_day_volume_ratio(current_volume, bars.get(symbol, []))
        source_parts = []
        if symbol in stock_requested:
            source_parts.append("watchlist")
        if symbol in mover_symbols:
            source_parts.append("mover")
        if symbol in active_symbols:
            source_parts.append("active")
        candidate = scout_candidate_payload(
            symbol=symbol,
            kind="stock",
            snapshot=snapshot,
            asset=assets.get(symbol),
            source=", ".join(source_parts) or "snapshot",
            news_count=news_counts.get(symbol, 0),
            volume_ratio=ratio,
        )
        if not candidate:
            continue
        if symbol in held_symbols:
            candidate["held_position"] = True
            candidate["reason"] = f"{candidate['reason']} Existing position detected."
        candidate["size"] = f"${min(max_order, buying_power):.2f} paper notional max"
        candidate_map[symbol] = candidate

    for symbol, snapshot in crypto_snapshots.items():
        candidate = scout_candidate_payload(
            symbol=symbol,
            kind="crypto",
            snapshot=snapshot,
            source="crypto snapshot",
            news_count=0,
            volume_ratio=None,
        )
        if not candidate:
            continue
        candidate["size"] = f"${min(max_order, buying_power):.2f} paper notional max"
        candidate_map[symbol] = candidate

    candidates = sorted(candidate_map.values(), key=lambda item: int(item.get("score") or 0), reverse=True)[:top]
    best = candidates[0] if candidates else None
    return {
        "configured": True,
        "status": "success",
        "engine": "alpaca-first",
        "uses_openai": False,
        "mode": "paper" if paper_mode else "live-locked",
        "risk": risk_settings,
        "buying_power": buying_power,
        "scanned": {
            "stocks": len(stock_symbols),
            "crypto": len(crypto_symbols),
            "watchlist": requested,
        },
        "best": best,
        "candidates": candidates,
        "warnings": warnings,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/watchlist/snapshots")
async def watchlist_snapshots(symbols: str | None = Query(default=None)) -> dict[str, object]:
    headers = alpaca_headers()
    requested = symbol_list(symbols, os.getenv("WATCHLIST_SYMBOLS", "TSLA,NVDA,SPY"))
    if not headers["APCA-API-KEY-ID"] or not headers["APCA-API-SECRET-KEY"]:
        return {
            "configured": False,
            "symbols": requested,
            "items": [],
            "message": "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env to load watched ticker snapshots.",
        }

    data_base_url = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")
    trading_base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2").rstrip("/")
    stock_symbols, crypto_symbols = watch_symbol_groups(",".join(requested))
    warnings: list[str] = []

    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        stock_snapshots, asset_names = await asyncio.gather(
            fetch_stock_snapshots(client, data_base_url, stock_symbols, warnings),
            fetch_asset_names(client, trading_base_url, stock_symbols),
        )
        crypto_snapshots: dict[str, dict[str, object]] = {}
        if crypto_symbols:
            response = await client.get(
                f"{data_base_url}/v1beta3/crypto/us/snapshots",
                params={"symbols": ",".join(crypto_symbols)},
            )
            if response.is_error:
                warnings.append(f"Alpaca crypto snapshots returned HTTP {response.status_code}.")
            else:
                crypto_snapshots = response.json().get("snapshots", {})

    items: list[dict[str, object]] = []
    for symbol in requested:
        crypto_pair = CRYPTO_SYMBOL_ALIASES.get(symbol, symbol if "/" in symbol else "")
        if crypto_pair:
            payload = market_item_payload(
                {
                    "label": crypto_pair.split("/", 1)[0],
                    "symbol": crypto_pair,
                    "kind": "crypto",
                    "href": f"https://www.cnbc.com/quotes/{crypto_pair.split('/', 1)[0]}.CM=",
                },
                crypto_snapshots,
            )
            payload["display_symbol"] = symbol
            items.append(payload)
        else:
            payload = stock_snapshot_payload(symbol, stock_snapshots.get(symbol, {}), asset_names.get(symbol))
            payload["display_symbol"] = symbol
            items.append(payload)

    return {
        "configured": True,
        "source": "Alpaca snapshots",
        "symbols": requested,
        "items": items,
        "warnings": warnings,
    }


@app.get("/api/alpaca/news")
async def alpaca_news(
    symbols: str | None = Query(default=None),
    limit: int = Query(default=8, ge=1, le=20),
) -> dict[str, object]:
    headers = alpaca_headers()
    if not headers["APCA-API-KEY-ID"] or not headers["APCA-API-SECRET-KEY"]:
        return {
            "configured": False,
            "message": "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env to load Alpaca news.",
            "articles": [],
            "tickers": [],
        }

    query_symbols = symbol_list(symbols, os.getenv("ALPACA_NEWS_SYMBOLS", "TSLA,AAPL,NVDA,SPY,QQQ"))
    data_base_url = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")
    params: dict[str, object] = {"limit": limit, "sort": "desc"}
    if query_symbols:
        params["symbols"] = ",".join(query_symbols)

    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        response = await client.get(f"{data_base_url}/v1beta1/news", params=params)
        if response.is_error:
            return {
                "configured": True,
                "status": "error",
                "message": f"Alpaca news returned HTTP {response.status_code}: {response.text[:240]}",
                "articles": [],
                "tickers": [],
            }
        data = response.json()

    ticker_counts: dict[str, int] = {}
    articles = []
    for item in data.get("news", [])[:limit]:
        item_symbols = [str(symbol).upper() for symbol in item.get("symbols", [])]
        for symbol in item_symbols:
            ticker_counts[symbol] = ticker_counts.get(symbol, 0) + 1
        sentiment = analyze_news_sentiment(
            item.get("headline"),
            item.get("summary"),
            " ".join(item_symbols),
        )
        articles.append(
            {
                "headline": item.get("headline"),
                "source": item.get("source"),
                "url": item.get("url"),
                "created_at": item.get("created_at"),
                "symbols": item_symbols,
                "summary": item.get("summary"),
                **sentiment,
            }
        )

    return {
        "configured": True,
        "status": "success",
        "query_symbols": query_symbols,
        "articles": articles,
        "tickers": [
            {"symbol": symbol, "count": count}
            for symbol, count in sorted(ticker_counts.items(), key=lambda item: item[1], reverse=True)
        ],
    }


@app.post("/api/research/notes")
async def create_research_note(payload: dict[str, object] = Body(...)) -> dict[str, object]:
    try:
        note = await asyncio.to_thread(
            add_note,
            title=str(payload.get("title") or ""),
            body=str(payload.get("body") or ""),
            tickers=payload.get("tickers") if isinstance(payload.get("tickers"), list) else str(payload.get("tickers") or ""),
            note_type=str(payload.get("note_type") or "note"),
            sentiment=str(payload.get("sentiment") or "neutral"),
            conviction=int(payload.get("conviction") or 3),
            horizon=str(payload.get("horizon") or ""),
            source_url=str(payload.get("source_url") or ""),
            tags=payload.get("tags") if isinstance(payload.get("tags"), list) else str(payload.get("tags") or ""),
        )
        return {"status": "saved", "note": note}
    except Exception as exc:
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}


@app.get("/api/research/notes")
async def list_research_notes(
    q: str = Query(default=""),
    ticker: str = Query(default=""),
    note_type: str = Query(default=""),
    sentiment: str = Query(default=""),
    limit: int = Query(default=25, ge=1, le=100),
) -> dict[str, object]:
    notes = await asyncio.to_thread(search_notes, q, ticker, note_type, sentiment, limit)
    return {"status": "ok", "count": len(notes), "notes": notes}


@app.get("/api/research/summary")
async def research_summary(ticker: str = Query(...), limit: int = Query(default=25, ge=1, le=100)) -> dict[str, object]:
    return await asyncio.to_thread(summarize_ticker, ticker, limit)


@app.delete("/api/research/notes/{note_id}")
async def remove_research_note(note_id: int) -> dict[str, object]:
    removed = await asyncio.to_thread(delete_note, note_id)
    return {"status": "deleted" if removed else "missing", "id": note_id}


@app.get("/api/company/scan")
async def company_scan(url: str = Query(...), max_chars: int = Query(default=6000, ge=1000, le=20000)) -> dict[str, object]:
    try:
        return await asyncio.to_thread(scan_company_website, url, max_chars)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@app.get("/api/newsdata/latest")
async def newsdata_latest(
    q: str | None = Query(default=None),
    language: str | None = Query(default=None),
    country: str | None = Query(default=None),
    category: str | None = Query(default=None),
    size: int = Query(default=10, ge=1, le=50),
) -> dict[str, object]:
    api_key = os.getenv("NEWSDATA_API_KEY", "").strip()
    if not api_key:
        return {
            "configured": False,
            "results": [],
            "message": "Set NEWSDATA_API_KEY in .env to load Newsdata.io headlines.",
        }

    q = q if isinstance(q, str) and q.strip() else None
    language = language if isinstance(language, str) and language.strip() else None
    country = country if isinstance(country, str) and country.strip() else None
    category = category if isinstance(category, str) and category.strip() else None

    params: dict[str, object] = {
        "apikey": api_key,
        "q": q or os.getenv("NEWSDATA_QUERY", "stock market OR crypto OR earnings"),
        "language": language or os.getenv("NEWSDATA_LANGUAGE", "en"),
        "country": country or os.getenv("NEWSDATA_COUNTRY", "us"),
        "size": size,
    }
    params["category"] = category or os.getenv("NEWSDATA_CATEGORY", "business")

    base_url = os.getenv("NEWSDATA_BASE_URL", "https://newsdata.io/api/1").rstrip("/")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{base_url}/latest", params=params)
        if response.is_error:
            return {
                "configured": True,
                "status": "error",
                "results": [],
                "message": f"Newsdata.io returned HTTP {response.status_code}: {response.text[:240]}",
            }
        data = response.json()

    articles = []
    for item in data.get("results", [])[:size]:
        sentiment = analyze_news_sentiment(
            item.get("title"),
            item.get("description"),
            item.get("source_name") or item.get("source_id"),
        )
        articles.append(
            {
                "title": item.get("title"),
                "source": item.get("source_name") or item.get("source_id"),
                "link": item.get("link"),
                "published": item.get("pubDate"),
                "provider_sentiment": item.get("sentiment"),
                **sentiment,
            }
        )

    return {
        "configured": True,
        "status": data.get("status"),
        "total_results": data.get("totalResults"),
        "results": articles,
    }


@app.websocket("/ws/agent")
async def agent_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            payload = await websocket.receive_json()
            prompt = (payload.get("prompt") or "").strip()
            if not prompt:
                await websocket.send_json({"type": "error", "error": "Prompt is required."})
                continue
            if not os.getenv("OPENAI_API_KEY"):
                await websocket.send_json(
                    {"type": "error", "error": "Set OPENAI_API_KEY before running the agent."}
                )
                continue

            model = payload.get("model") or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
            config_path = payload.get("config_path") or "mcp_config.json"
            await websocket.send_json({"type": "status", "message": "Connecting MCP servers..."})

            try:
                async with OpenAIMCPAgent(model=model, config_path=config_path) as agent:
                    if payload.get("registry_only"):
                        await websocket.send_json(
                            {
                                "type": "registry",
                                "model": agent.model,
                                "servers": list(agent.servers),
                                "tools": [tool["name"] for tool in agent.openai_tools],
                            }
                        )
                        for warning in agent.connection_warnings:
                            await websocket.send_json({"type": "warning", "message": warning})
                        await websocket.send_json({"type": "done"})
                        continue

                    async for event in agent.run(prompt):
                        await websocket.send_json(event)
            except Exception as exc:
                await websocket.send_json({"type": "error", "error": user_facing_error(exc)})
    except WebSocketDisconnect:
        return
