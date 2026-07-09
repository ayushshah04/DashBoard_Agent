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


def returns_from_bars(bars: Iterable[dict[str, object]]) -> list[float]:
    closes = [to_float(bar.get("c")) for bar in bars if isinstance(bar, dict)]
    closes = [close for close in closes if close and close > 0]
    return [((closes[index] / closes[index - 1]) - 1) * 100 for index in range(1, len(closes))]


def trend_from_bars(bars: Iterable[dict[str, object]], direction: object = "long") -> dict[str, object]:
    closes = [to_float(bar.get("c")) for bar in bars if isinstance(bar, dict)]
    closes = [close for close in closes if close and close > 0]
    if len(closes) < 8:
        return {"status": "limited_history", "score": 5, "label": "limited trend data"}
    short_window = closes[-5:]
    long_window = closes[-15:] if len(closes) >= 15 else closes
    short_avg = sum(short_window) / len(short_window)
    long_avg = sum(long_window) / len(long_window)
    slope = ((closes[-1] / closes[-min(6, len(closes))]) - 1) * 100
    sign = -1 if str(direction or "").lower().startswith("short") else 1
    aligned = (short_avg - long_avg) * sign > 0 and slope * sign > 0
    score = 13 if aligned else 7 if (short_avg - long_avg) * sign > 0 else 3
    label = "aligned uptrend" if aligned and sign > 0 else "aligned downtrend" if aligned else "mixed trend"
    return {
        "status": "ok",
        "score": score,
        "label": label,
        "short_average": round(short_avg, 4),
        "long_average": round(long_avg, 4),
        "six_bar_slope_percent": round(slope, 3),
    }


def correlation_from_bars(left: Iterable[dict[str, object]], right: Iterable[dict[str, object]]) -> float | None:
    left_returns = returns_from_bars(left)
    right_returns = returns_from_bars(right)
    sample = min(len(left_returns), len(right_returns))
    if sample < 6:
        return None
    x_values = left_returns[-sample:]
    y_values = right_returns[-sample:]
    x_avg = sum(x_values) / sample
    y_avg = sum(y_values) / sample
    numerator = sum((x - x_avg) * (y - y_avg) for x, y in zip(x_values, y_values))
    x_var = sum((x - x_avg) ** 2 for x in x_values)
    y_var = sum((y - y_avg) ** 2 for y in y_values)
    if not x_var or not y_var:
        return None
    return round(numerator / math.sqrt(x_var * y_var), 3)


def backtest_from_bars(bars: Iterable[dict[str, object]], direction: object = "long") -> dict[str, object]:
    returns = returns_from_bars(bars)
    if len(returns) < 5:
        return {
            "status": "limited_history",
            "sample_size": len(returns),
            "win_rate_percent": None,
            "average_return_percent": None,
            "max_drawdown_percent": None,
            "volatility_percent": None,
            "sharpe_proxy": None,
            "profit_factor": None,
        }

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


def execution_quality(candidate: dict[str, object]) -> dict[str, object]:
    asset_class = str(candidate.get("asset_class") or "").lower()
    price = to_float(candidate.get("price") or candidate.get("entry")) or 0
    volume = to_float(candidate.get("volume")) or 0
    spread = to_float(candidate.get("spread_percent"))
    if spread is None:
        bid = to_float(candidate.get("bid"))
        ask = to_float(candidate.get("ask"))
        if bid and ask and ask >= bid:
            midpoint = (ask + bid) / 2
            spread = ((ask - bid) / midpoint * 100) if midpoint else None

    if asset_class == "crypto":
        max_spread = 0.75
    elif price and price < 5:
        max_spread = 1.50
    elif volume >= 1_000_000:
        max_spread = 0.35
    else:
        max_spread = 0.75

    spread_ok = spread is None or spread <= max_spread
    thin = asset_class != "crypto" and (volume < 250_000 or (price and price < 5) or (spread is not None and spread > 0.35))
    order_type = "limit" if thin or not spread_ok else "market"
    entry = to_float(candidate.get("entry") or candidate.get("price")) or price
    ask = to_float(candidate.get("ask"))
    limit_price = ask or entry
    if limit_price and price and order_type == "limit":
        limit_price = min(limit_price, price * 1.005)
    digits = 4 if limit_price and limit_price < 1 else 2

    return {
        "spread_percent": round(spread, 4) if spread is not None else None,
        "max_allowed_spread_percent": max_spread,
        "spread_ok": spread_ok,
        "liquidity_bucket": "liquid" if volume >= 1_000_000 and not thin else "thin" if thin else "normal",
        "recommended_order_type": order_type,
        "limit_price": f"{limit_price:.{digits}f}" if limit_price else None,
        "reason": "Use limit order for thin/wide-spread execution." if order_type == "limit" else "Market order acceptable for liquid/tight-spread execution.",
    }


def factor_scores(candidate: dict[str, object]) -> dict[str, int]:
    direction = str(candidate.get("direction") or "long").lower()
    status = str(candidate.get("status") or "").lower()
    asset_class = str(candidate.get("asset_class") or "").lower()
    change = to_float(candidate.get("change_percent")) or 0
    volume_ratio = to_float(candidate.get("volume_ratio")) or 0
    news_count = int(to_float(candidate.get("news_count")) or 0)
    sentiment_raw = to_float(candidate.get("news_sentiment_score")) or 0
    volume = to_float(candidate.get("volume")) or 0
    tradable = bool(candidate.get("tradable", True))
    backtest = candidate.get("backtest_metrics") if isinstance(candidate.get("backtest_metrics"), dict) else {}
    trend = candidate.get("trend_metrics") if isinstance(candidate.get("trend_metrics"), dict) else {}
    execution = candidate.get("execution_quality") if isinstance(candidate.get("execution_quality"), dict) else {}

    aligned = change < 0 if direction.startswith("short") else change > 0
    momentum = min(20, int(abs(change) * 2.5)) if aligned else max(0, 6 - int(abs(change) * 2))
    volume_surprise = min(14, int(volume_ratio * 3)) if volume_ratio else 4
    news_sentiment = int(clamp(5 + sentiment_raw / 2 + min(2, news_count), 0, 10))
    volatility_value = to_float(backtest.get("volatility_percent")) or to_float(candidate.get("volatility_percent")) or 0
    if 1 <= volatility_value <= 8:
        volatility = 10
    elif 8 < volatility_value <= 18:
        volatility = 7
    elif volatility_value > 18:
        volatility = 3
    else:
        volatility = 5

    if asset_class == "crypto":
        liquidity = 10
    elif volume >= 1_000_000:
        liquidity = 12
    elif volume >= 250_000:
        liquidity = 10
    elif volume >= 50_000:
        liquidity = 7
    else:
        liquidity = 3
    if execution.get("spread_ok") is False:
        liquidity = max(0, liquidity - 5)

    tradeability = 10 if tradable and "blocked" not in status else 0
    if direction.startswith("short") and not (candidate.get("shortable") and candidate.get("easy_to_borrow")):
        tradeability = min(tradeability, 4)

    entry = to_float(candidate.get("entry") or candidate.get("price"))
    stop = to_float(candidate.get("stop"))
    risk_quality = 4
    if entry and stop and entry > 0 and stop > 0 and entry != stop:
        risk_percent = abs(entry - stop) / entry * 100
        if 0.25 <= risk_percent <= 4:
            risk_quality = 10
        elif risk_percent <= 8:
            risk_quality = 7
        else:
            risk_quality = 4
    trend_score = int(to_float(trend.get("score")) or 5)

    return {
        "momentum": int(clamp(momentum, 0, 20)),
        "volume_surprise": int(clamp(volume_surprise, 0, 14)),
        "volatility": int(clamp(volatility, 0, 10)),
        "liquidity": int(clamp(liquidity, 0, 12)),
        "news_sentiment": int(clamp(news_sentiment, 0, 10)),
        "trend": int(clamp(trend_score, 0, 13)),
        "tradeability": int(clamp(tradeability, 0, 10)),
        "risk_quality": int(clamp(risk_quality, 0, 10)),
    }


def risk_penalty(candidate: dict[str, object], risk_settings: dict[str, object]) -> tuple[int, list[str]]:
    penalty = 0
    reasons: list[str] = []
    status = str(candidate.get("status") or "").lower()
    direction = str(candidate.get("direction") or "").lower()
    portfolio = candidate.get("portfolio") if isinstance(candidate.get("portfolio"), dict) else {}
    execution = candidate.get("execution_quality") if isinstance(candidate.get("execution_quality"), dict) else {}
    risk = candidate.get("risk") if isinstance(candidate.get("risk"), dict) else {}
    daily_loss = to_float(portfolio.get("daily_loss_usd")) or 0
    daily_limit = to_float(portfolio.get("daily_loss_limit_usd")) or risk_number(risk_settings, "max_daily_loss_usd", 250)

    if "skip" in status:
        penalty += 30
        reasons.append("candidate is marked skipped")
    if "blocked" in status:
        penalty += 60
        reasons.append("candidate is blocked")
    if direction.startswith("short") and not (candidate.get("shortable") and candidate.get("easy_to_borrow")):
        penalty += 20
        reasons.append("short borrow not confirmed")
    if portfolio.get("held_position"):
        penalty += 8
        reasons.append("existing position")
    if portfolio.get("open_order"):
        penalty += 35
        reasons.append("open order already exists")
    if (to_float(portfolio.get("sector_exposure_percent")) or 0) > 35:
        penalty += 8
        reasons.append("sector concentration")
    if abs(to_float(portfolio.get("max_correlation")) or 0) > 0.80:
        penalty += 8
        reasons.append("high correlation to holdings")
    if daily_limit and daily_loss >= daily_limit:
        penalty += 65
        reasons.append("daily loss limit reached")
    if execution.get("spread_ok") is False:
        penalty += 35
        reasons.append("spread too wide")
    if not risk.get("suggested_notional") or to_float(risk.get("suggested_notional")) < 1:
        penalty += 50
        reasons.append("risk size below minimum")
    return penalty, reasons


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
            "formula": "shares = max_risk_per_trade / abs(entry - stop)",
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
        "formula": "shares = max_risk_per_trade / abs(entry - stop)",
        "display_size": f"{fmt_money(suggested_notional)} quant risk cap / est {fmt_qty(estimated_qty)} shares",
    }


def enrich_candidate(
    candidate: dict[str, object],
    risk_settings: dict[str, object],
    buying_power: object,
    bars: Iterable[dict[str, object]] | None = None,
) -> dict[str, object]:
    enriched = dict(candidate)
    enriched["execution_quality"] = execution_quality(enriched)
    factors = factor_scores(enriched)
    base_score = sum(factors.values())
    penalty = 0
    status = str(enriched.get("status") or "").lower()

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
    enriched["risk"] = risk
    risk_penalty_value, penalty_reasons = risk_penalty(enriched, risk_settings)
    penalty += risk_penalty_value

    quant_score = int(clamp(base_score - penalty, 0, 100))
    enriched["engine"] = "quant-v2"
    enriched["alpaca_score"] = enriched.get("score")
    enriched["score"] = quant_score
    enriched["quant_score"] = quant_score
    enriched["factor_scores"] = factors
    enriched["risk_penalty"] = {"score": penalty, "reasons": penalty_reasons}
    enriched["backtest_metrics"] = backtest
    enriched["size"] = risk["display_size"]
    enriched["suggested_order_type"] = enriched["execution_quality"]["recommended_order_type"]
    enriched["limit_price"] = enriched["execution_quality"]["limit_price"]
    executable = quant_score >= 65 and "skip" not in status and "blocked" not in status and not penalty_reasons.count("open order already exists")
    executable = executable and enriched["execution_quality"].get("spread_ok") is not False
    enriched["execution_route"] = "alpaca_order_api" if executable else "watch_only"
    if "skip" in status or "blocked" in status:
        enriched["outcome"] = "skipped" if "skip" in status else "blocked"
    elif executable:
        enriched["status"] = "Staged ticket"
        enriched["outcome"] = "open"
    else:
        enriched["status"] = "Watch"
        enriched["outcome"] = "open"
    enriched["quant_reason"] = (
        f"Quant v2 score {quant_score}/100 from momentum {factors['momentum']}, volume surprise {factors['volume_surprise']}, "
        f"volatility {factors['volatility']}, liquidity {factors['liquidity']}, sentiment {factors['news_sentiment']}, "
        f"trend {factors['trend']}, risk quality {factors['risk_quality']}; penalty {penalty}."
    )
    enriched["reason"] = f"{enriched.get('reason') or ''} {enriched['quant_reason']}".strip()
    enriched["model_confidence"] = model_confidence(enriched)
    return enriched


def model_confidence(candidate: dict[str, object]) -> dict[str, object]:
    score = int(to_float(candidate.get("quant_score")) or 0)
    backtest = candidate.get("backtest_metrics") if isinstance(candidate.get("backtest_metrics"), dict) else {}
    execution = candidate.get("execution_quality") if isinstance(candidate.get("execution_quality"), dict) else {}
    penalty = candidate.get("risk_penalty") if isinstance(candidate.get("risk_penalty"), dict) else {}
    points = score
    if backtest.get("status") == "ok" and (to_float(backtest.get("sample_size")) or 0) >= 10:
        points += 5
    if execution.get("spread_ok") is False:
        points -= 20
    points -= min(20, int(to_float(penalty.get("score")) or 0) // 3)
    points = int(clamp(points, 0, 100))
    label = "High" if points >= 75 else "Medium" if points >= 55 else "Low"
    return {"score": points, "label": label}


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
