from __future__ import annotations

import math
import re
from typing import Iterable


def to_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        number = float(match.group(0))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def fmt_money(value: float) -> str:
    return f"${value:.2f}"


def fmt_qty(value: float) -> str:
    if value >= 100:
        return f"{value:.0f}"
    if value >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def risk_number(risk_settings: dict[str, object], key: str, default: float) -> float:
    return to_float(risk_settings.get(key)) or default


def backtest_from_bars(bars: Iterable[dict[str, object]], direction: object = "long") -> dict[str, object]:
    closes = [to_float(bar.get("c")) for bar in bars if isinstance(bar, dict)]
    closes = [close for close in closes if close and close > 0]
    if len(closes) < 6:
        return {
            "status": "limited_history",
            "sample_size": max(0, len(closes) - 1),
            "win_rate_percent": None,
            "average_return_percent": None,
            "max_drawdown_percent": None,
            "volatility_percent": None,
            "sharpe_proxy": None,
            "profit_factor": None,
        }

    returns = [((closes[index] / closes[index - 1]) - 1) * 100 for index in range(1, len(closes))]
    sign = -1 if str(direction or "").lower().startswith("short") else 1
    signal_returns = [ret * sign for ret in returns]
    wins = [ret for ret in signal_returns if ret > 0]
    losses = [ret for ret in signal_returns if ret < 0]
    average = sum(signal_returns) / len(signal_returns)
    variance = sum((ret - average) ** 2 for ret in signal_returns) / max(1, len(signal_returns) - 1)
    volatility = math.sqrt(variance)
    sharpe = (average / volatility * math.sqrt(252)) if volatility else None

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for ret in signal_returns:
        equity *= 1 + (ret / 100)
        peak = max(peak, equity)
        if peak:
            max_drawdown = min(max_drawdown, (equity / peak - 1) * 100)

    gross_gain = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_gain / gross_loss) if gross_loss else (gross_gain if gross_gain else None)

    return {
        "status": "ok",
        "sample_size": len(signal_returns),
        "win_rate_percent": round(len(wins) / len(signal_returns) * 100, 2),
        "average_return_percent": round(average, 3),
        "max_drawdown_percent": round(max_drawdown, 2),
        "volatility_percent": round(volatility, 3),
        "sharpe_proxy": round(sharpe, 2) if sharpe is not None else None,
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
    }


def factor_scores(candidate: dict[str, object]) -> dict[str, int]:
    direction = str(candidate.get("direction") or "long").lower()
    status = str(candidate.get("status") or "").lower()
    asset_class = str(candidate.get("asset_class") or "").lower()
    change = to_float(candidate.get("change_percent")) or 0
    volume_ratio = to_float(candidate.get("volume_ratio")) or 0
    news_count = int(to_float(candidate.get("news_count")) or 0)
    volume = to_float(candidate.get("volume")) or 0
    tradable = bool(candidate.get("tradable", True))

    aligned = change < 0 if direction.startswith("short") else change > 0
    momentum = min(25, int(abs(change) * 3.0)) if aligned else max(0, 8 - int(abs(change) * 2))
    volume_surprise = min(20, int(volume_ratio * 4)) if volume_ratio else 5
    news = min(10, news_count * 3)

    if asset_class == "crypto":
        liquidity = 11
    elif volume >= 1_000_000:
        liquidity = 15
    elif volume >= 250_000:
        liquidity = 12
    elif volume >= 50_000:
        liquidity = 8
    else:
        liquidity = 4

    tradeability = 15 if tradable and "blocked" not in status else 0
    if direction.startswith("short") and not (candidate.get("shortable") and candidate.get("easy_to_borrow")):
        tradeability = min(tradeability, 4)

    entry = to_float(candidate.get("entry") or candidate.get("price"))
    stop = to_float(candidate.get("stop"))
    risk_quality = 4
    if entry and stop and entry > 0 and stop > 0 and entry != stop:
        risk_percent = abs(entry - stop) / entry * 100
        if 0.25 <= risk_percent <= 4:
            risk_quality = 15
        elif risk_percent <= 8:
            risk_quality = 10
        else:
            risk_quality = 6

    return {
        "momentum": int(clamp(momentum, 0, 25)),
        "volume": int(clamp(volume_surprise, 0, 20)),
        "news": int(clamp(news, 0, 10)),
        "liquidity": int(clamp(liquidity, 0, 15)),
        "tradeability": int(clamp(tradeability, 0, 15)),
        "risk_quality": int(clamp(risk_quality, 0, 15)),
    }


def estimate_position_size(
    candidate: dict[str, object],
    risk_settings: dict[str, object],
    buying_power: object,
) -> dict[str, object]:
    entry = to_float(candidate.get("entry") or candidate.get("price"))
    stop = to_float(candidate.get("stop"))
    max_order = risk_number(risk_settings, "max_order_notional_usd", 50)
    max_position = risk_number(risk_settings, "max_position_usd", max_order)
    max_daily_loss = risk_number(risk_settings, "max_daily_loss_usd", 250)
    power = to_float(buying_power) or max_order
    max_notional = max(0, min(max_order, max_position, power))
    max_risk = max(1, min(max_daily_loss * 0.05, max_order * 0.02, max_position * 0.02))

    if not entry or entry <= 0:
        return {
            "method": "notional_cap",
            "suggested_notional": round(max_notional, 2),
            "estimated_quantity": None,
            "max_risk_usd": round(max_risk, 2),
            "risk_per_unit": None,
            "risk_percent": None,
            "display_size": f"{fmt_money(max_notional)} quant notional cap",
        }

    risk_per_unit = abs(entry - stop) if stop and stop > 0 else entry * 0.01
    risk_percent = risk_per_unit / entry * 100 if entry else None
    qty_by_risk = max_risk / risk_per_unit if risk_per_unit > 0 else max_notional / entry
    qty_by_notional = max_notional / entry if max_notional > 0 else 0
    estimated_qty = max(0, min(qty_by_risk, qty_by_notional))
    suggested_notional = min(max_notional, estimated_qty * entry)

    return {
        "method": "risk_per_unit",
        "suggested_notional": round(suggested_notional, 2),
        "estimated_quantity": round(estimated_qty, 6),
        "max_risk_usd": round(max_risk, 2),
        "risk_per_unit": round(risk_per_unit, 4),
        "risk_percent": round(risk_percent, 3) if risk_percent is not None else None,
        "display_size": f"{fmt_money(suggested_notional)} quant risk cap / est {fmt_qty(estimated_qty)} shares",
    }


def enrich_candidate(
    candidate: dict[str, object],
    risk_settings: dict[str, object],
    buying_power: object,
    bars: Iterable[dict[str, object]] | None = None,
) -> dict[str, object]:
    enriched = dict(candidate)
    factors = factor_scores(enriched)
    base_score = sum(factors.values())
    penalty = 0
    status = str(enriched.get("status") or "").lower()
    direction = str(enriched.get("direction") or "").lower()
    if "skip" in status:
        penalty += 30
    if "blocked" in status:
        penalty += 60
    if direction.startswith("short") and not (enriched.get("shortable") and enriched.get("easy_to_borrow")):
        penalty += 20
    if enriched.get("held_position"):
        penalty += 8

    backtest = backtest_from_bars(bars or [], enriched.get("direction"))
    if backtest.get("status") != "ok":
        backtest = dict(enriched.get("backtest_metrics") or backtest)
    if backtest.get("status") == "ok":
        win_rate = to_float(backtest.get("win_rate_percent")) or 0
        sharpe = to_float(backtest.get("sharpe_proxy")) or 0
        drawdown = abs(to_float(backtest.get("max_drawdown_percent")) or 0)
        base_score += int(clamp((win_rate - 45) / 2, -8, 10))
        base_score += int(clamp(sharpe * 2, -6, 8))
        penalty += int(clamp(drawdown / 6, 0, 10))

    risk = estimate_position_size(enriched, risk_settings, buying_power)
    if not risk.get("suggested_notional") or to_float(risk.get("suggested_notional")) < 1:
        penalty += 50

    quant_score = int(clamp(base_score - penalty, 0, 100))
    enriched["engine"] = "quant-v1"
    enriched["alpaca_score"] = enriched.get("score")
    enriched["score"] = quant_score
    enriched["quant_score"] = quant_score
    enriched["factor_scores"] = factors
    enriched["risk"] = risk
    enriched["backtest_metrics"] = backtest
    enriched["size"] = risk["display_size"]
    enriched["execution_route"] = "alpaca_order_api" if quant_score >= 65 and "skip" not in status and "blocked" not in status else "watch_only"
    if "skip" in status or "blocked" in status:
        enriched["outcome"] = "skipped" if "skip" in status else "blocked"
    elif quant_score >= 65:
        enriched["status"] = "Staged ticket"
        enriched["outcome"] = "open"
    else:
        enriched["status"] = "Watch"
        enriched["outcome"] = "open"
    enriched["quant_reason"] = (
        f"Quant score {quant_score}/100 from momentum {factors['momentum']}, volume {factors['volume']}, "
        f"liquidity {factors['liquidity']}, news {factors['news']}, risk quality {factors['risk_quality']}."
    )
    enriched["reason"] = f"{enriched.get('reason') or ''} {enriched['quant_reason']}".strip()
    return enriched


def rank_candidates(
    candidates: Iterable[dict[str, object]],
    risk_settings: dict[str, object],
    buying_power: object,
    *,
    top: int = 8,
) -> list[dict[str, object]]:
    enriched = [enrich_candidate(candidate, risk_settings, buying_power) for candidate in candidates]
    enriched.sort(key=lambda item: (int(item.get("quant_score") or 0), int(item.get("alpaca_score") or 0)), reverse=True)
    for index, item in enumerate(enriched, start=1):
        item["quant_rank"] = index
    return enriched[:top]
