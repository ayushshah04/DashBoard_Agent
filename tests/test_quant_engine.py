from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import quant_engine
import server


def async_run(coro):
    return asyncio.run(coro)


def sample_candidate(symbol="MOVE", change=6.0, volume_ratio=8.0, status="Staged ticket"):
    return {
        "symbol": symbol,
        "asset_class": "equity",
        "direction": "long",
        "status": status,
        "score": 72,
        "price": 10.0,
        "change_percent": change,
        "volume": 1_250_000,
        "volume_ratio": volume_ratio,
        "news_count": 2,
        "tradable": True,
        "entry": "$10.00",
        "exit_target": "$10.20",
        "stop": "$9.90",
        "reason": "test candidate",
    }


class QuantEngineTests(unittest.TestCase):
    def test_backtest_from_bars_returns_compact_metrics(self):
        bars = [{"c": value} for value in [10, 10.2, 10.1, 10.5, 10.8, 10.7, 11.0]]
        metrics = quant_engine.backtest_from_bars(bars, "long")
        self.assertEqual(metrics["status"], "ok")
        self.assertEqual(metrics["sample_size"], 6)
        self.assertGreater(metrics["win_rate_percent"], 50)
        self.assertIn("max_drawdown_percent", metrics)

    def test_rank_candidates_applies_factor_score_and_risk_cap(self):
        good = sample_candidate("GOOD")
        skipped = sample_candidate("DROP", change=-8, volume_ratio=20, status="Skipped")
        skipped["direction"] = "skip-short"
        ranked = quant_engine.rank_candidates(
            [skipped, good],
            {
                "max_order_notional_usd": "10000",
                "max_position_usd": "100",
                "max_daily_loss_usd": "500",
            },
            1000,
            top=2,
        )
        self.assertEqual(ranked[0]["symbol"], "GOOD")
        self.assertEqual(ranked[0]["status"], "Staged ticket")
        self.assertEqual(ranked[0]["risk"]["suggested_notional"], 100)
        self.assertIn("quant risk cap", ranked[0]["size"])
        self.assertLess(ranked[1]["quant_score"], ranked[0]["quant_score"])

    def test_quant_scout_endpoint_reranks_alpaca_universe(self):
        async def fake_alpaca_scout(symbols=None, top=8, include_crypto=True):
            return {
                "configured": True,
                "status": "success",
                "engine": "alpaca-first",
                "uses_openai": False,
                "risk": {
                    "max_order_notional_usd": "10000",
                    "max_position_usd": "100",
                    "max_daily_loss_usd": "500",
                },
                "buying_power": 1000,
                "scanned": {"stocks": 2, "crypto": 0, "watchlist": ["GOOD", "DROP"]},
                "best": sample_candidate("DROP", change=-8, volume_ratio=20, status="Skipped"),
                "candidates": [
                    sample_candidate("DROP", change=-8, volume_ratio=20, status="Skipped"),
                    sample_candidate("GOOD", change=6, volume_ratio=8, status="Staged ticket"),
                ],
                "warnings": [],
            }

        with patch.object(server, "alpaca_scout", fake_alpaca_scout):
            result = async_run(server.quant_scout(symbols="GOOD,DROP", top=3, include_crypto=True))

        self.assertEqual(result["engine"], "quant-v1")
        self.assertEqual(result["raw_engine"], "alpaca-first")
        self.assertEqual(result["best"]["symbol"], "GOOD")
        self.assertEqual(result["candidates"][0]["quant_rank"], 1)
        self.assertEqual(result["candidates"][0]["status"], "Staged ticket")


if __name__ == "__main__":
    unittest.main()
