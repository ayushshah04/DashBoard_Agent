from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

import server


class FakeResponse:
    def __init__(self, payload, status_code: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else str(payload)

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self):
        return self._payload


class FakeAsyncClient:
    def __init__(self, get_routes=None, post_routes=None, *args, **kwargs):
        self.get_routes = list(get_routes or [])
        self.post_routes = list(post_routes or [])
        self.get_calls = []
        self.post_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if not self.get_routes:
            raise AssertionError(f"Unexpected GET {url}")
        matcher, response = self.get_routes.pop(0)
        if matcher and matcher not in url:
            raise AssertionError(f"Expected GET containing {matcher!r}, got {url!r}")
        return response

    async def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.post_routes:
            raise AssertionError(f"Unexpected POST {url}")
        matcher, response = self.post_routes.pop(0)
        if matcher and matcher not in url:
            raise AssertionError(f"Expected POST containing {matcher!r}, got {url!r}")
        return response


def async_run(coro):
    return asyncio.run(coro)


def fake_env(**overrides):
    values = {
        "ALPACA_API_KEY": "test-key",
        "ALPACA_SECRET_KEY": "test-secret",
        "ALPACA_BASE_URL": "https://paper-api.alpaca.markets/v2",
        "ALPACA_PAPER_TRADE": "true",
        "MAX_ORDER_NOTIONAL_USD": "50",
        "MAX_POSITION_USD": "100",
        "MAX_DAILY_LOSS_USD": "250",
    }
    values.update(overrides)
    return patch.dict(os.environ, values, clear=False)


def risk(order="50", position="100", daily_loss="250", options="1"):
    return {
        "max_order_notional_usd": order,
        "max_position_usd": position,
        "max_daily_loss_usd": daily_loss,
        "options_max_contracts": options,
    }


def no_open_orders():
    return ("/orders", FakeResponse([]))


class TradingRegressionTests(unittest.TestCase):
    def test_risk_settings_clamp_and_format(self):
        normalized = server.normalize_risk_settings(
            {
                "max_order_notional_usd": "999999999",
                "max_position_usd": "-10",
                "max_daily_loss_usd": "125.50",
                "options_max_contracts": "2.4",
            }
        )
        self.assertEqual(normalized["max_order_notional_usd"], "1000000")
        self.assertEqual(normalized["max_position_usd"], "1")
        self.assertEqual(normalized["max_daily_loss_usd"], "125.5")
        self.assertEqual(normalized["options_max_contracts"], "2")

    def test_parse_trade_numbers(self):
        self.assertEqual(server.parse_trade_number("$10,000.25 paper notional max"), 10000.25)
        self.assertEqual(server.parse_trade_number("85 qty"), 85)
        self.assertIsNone(server.parse_trade_number("not available"))

    def test_trading_status_declares_supported_and_proxy_markets(self):
        with fake_env(ALPACA_PAPER_TRADE="true"):
            status = server.trading_status()
        self.assertTrue(status["paper"])
        self.assertEqual(status["execution"]["summary"], "Direct: equities, ETFs, options, crypto | FX/oil: data or listed proxies")
        market_support = {item["market"]: item["support"] for item in status["execution"]["markets"]}
        self.assertEqual(market_support["FX/currency"], "data_or_proxy_only")
        self.assertEqual(market_support["Oil and commodities"], "proxy_only")

    def test_asset_class_inference_distinguishes_crypto_and_fx(self):
        self.assertEqual(server.infer_asset_class("BTC/USD", {}), "crypto")
        self.assertEqual(server.infer_asset_class("ETH/USD", {}), "crypto")
        self.assertEqual(server.infer_asset_class("EUR/USD", {}), "currency")
        self.assertEqual(server.infer_asset_class("FXE", {"asset_class": "equity"}), "equity")
        self.assertEqual(server.infer_asset_class("TSLA250117C00400000", {"asset_class": "option"}), "option")

    def test_order_record_normalizes_accepted_and_filled_status(self):
        accepted = server.alpaca_order_trade_record(
            {
                "id": "abc123",
                "symbol": "VTAK",
                "status": "accepted",
                "side": "buy",
                "qty": "85",
                "filled_qty": "0",
                "submitted_at": "2026-07-08T21:02:49Z",
                "expires_at": "2026-07-09T20:00:00Z",
            }
        )
        filled = server.alpaca_order_trade_record(
            {
                "id": "def456",
                "symbol": "CRNX",
                "status": "filled",
                "side": "buy",
                "notional": "49",
                "filled_qty": "0.35758602",
                "filled_avg_price": "83.538",
            }
        )
        self.assertEqual(accepted["status"], "Alpaca Accepted")
        self.assertEqual(accepted["quantity_or_notional"], "85 qty / filled 0/85")
        self.assertEqual(accepted["api_status"], "accepted")
        self.assertEqual(accepted["filled_qty"], "0")
        self.assertEqual(accepted["expires_at"], "2026-07-09T20:00:00Z")
        self.assertEqual(filled["status"], "Alpaca Filled")
        self.assertEqual(filled["entry"], "$83.538")
        self.assertEqual(filled["quantity_or_notional"], "$49 notional / filled 0.35758602")

    def test_orders_endpoint_syncs_trade_records(self):
        client = FakeAsyncClient(
            get_routes=[
                (
                    "/orders",
                    FakeResponse(
                        [
                            {"id": "abc123", "symbol": "VTAK", "status": "accepted", "side": "buy", "qty": "85"},
                            {"id": "def456", "symbol": "CRNX", "status": "filled", "side": "buy", "notional": "49"},
                        ]
                    ),
                )
            ]
        )
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client):
            result = async_run(server.alpaca_orders(status="all", limit=5))
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["trade_records"]), 2)
        self.assertEqual(result["trade_records"][0]["symbol"], "VTAK")
        self.assertEqual(result["trade_records"][0]["status"], "Alpaca Accepted")
        self.assertEqual(client.get_calls[0][1]["params"]["status"], "all")
        self.assertEqual(client.get_calls[0][1]["params"]["limit"], 5)

    def test_paper_order_blocks_missing_keys(self):
        with fake_env(ALPACA_API_KEY="", ALPACA_SECRET_KEY=""):
            result = async_run(server.alpaca_paper_order({"symbol": "TSLA", "status": "Staged ticket"}))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Set ALPACA_API_KEY", result["message"])

    def test_paper_order_blocks_live_mode(self):
        with fake_env(ALPACA_PAPER_TRADE="false"):
            result = async_run(server.alpaca_paper_order({"symbol": "TSLA", "status": "Staged ticket"}))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("paper-only", result["message"])

    def test_paper_order_blocks_unsupported_options(self):
        with fake_env():
            result = async_run(
                server.alpaca_paper_order({"symbol": "TSLA250117C00400000", "asset_class": "option", "status": "Staged ticket"})
            )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("supports Alpaca equities/ETFs and crypto only", result["message"])

    def test_paper_order_blocks_direct_fx_pair(self):
        with fake_env():
            result = async_run(
                server.alpaca_paper_order({"symbol": "EUR/USD", "asset_class": "currency", "status": "Staged ticket", "size": "$25"})
            )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("supports Alpaca equities/ETFs and crypto only", result["message"])

    def test_paper_order_blocks_skipped_watch_and_short_candidates(self):
        cases = [
            ({"symbol": "TSLA", "status": "Skipped"}, "no paper order submitted"),
            ({"symbol": "TSLA", "status": "Watch"}, "only staged tickets"),
            ({"symbol": "TSLA", "status": "Staged ticket", "direction": "short-watch"}, "long-only"),
        ]
        with fake_env():
            for candidate, expected in cases:
                with self.subTest(candidate=candidate):
                    result = async_run(server.alpaca_paper_order(candidate))
                    self.assertEqual(result["status"], "blocked")
                    self.assertIn(expected, result["message"])

    def test_paper_order_blocks_low_buying_power_before_asset_lookup(self):
        client = FakeAsyncClient(get_routes=[("/account", FakeResponse({"buying_power": "0.50"}))])
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="50", position="100")
        ):
            result = async_run(server.alpaca_paper_order({"symbol": "TSLA", "status": "Staged ticket", "size": "$50"}))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("below $1", result["message"])
        self.assertEqual(len(client.get_calls), 1)

    def test_paper_order_blocks_nontradable_asset(self):
        client = FakeAsyncClient(
            get_routes=[
                ("/account", FakeResponse({"buying_power": "1000"})),
                ("/assets/TSLA", FakeResponse({"tradable": False, "fractionable": True})),
            ]
        )
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="50", position="100")
        ):
            result = async_run(server.alpaca_paper_order({"symbol": "TSLA", "status": "Staged ticket", "size": "$50"}))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("not marked tradable", result["message"])

    def test_paper_order_blocks_nonfractionable_too_small(self):
        client = FakeAsyncClient(
            get_routes=[
                ("/account", FakeResponse({"buying_power": "1000"})),
                ("/assets/BRK.A", FakeResponse({"tradable": True, "fractionable": False})),
            ]
        )
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="50", position="100")
        ):
            result = async_run(server.alpaca_paper_order({"symbol": "BRK.A", "status": "Staged ticket", "size": "$50", "entry": "$600000"}))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("too small to buy one share", result["message"])

    def test_paper_order_submits_fractionable_equity_notional_capped_by_position(self):
        order_payload = {
            "id": "order-1",
            "symbol": "TSLA",
            "status": "accepted",
            "side": "buy",
            "notional": "100.00",
        }
        client = FakeAsyncClient(
            get_routes=[
                ("/account", FakeResponse({"buying_power": "5000"})),
                ("/assets/TSLA", FakeResponse({"tradable": True, "fractionable": True})),
                no_open_orders(),
            ],
            post_routes=[("/orders", FakeResponse(order_payload))],
        )
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="10000", position="100")
        ):
            result = async_run(server.alpaca_paper_order({"symbol": "TSLA", "status": "Staged ticket", "size": "$10000"}))
        self.assertEqual(result["status"], "submitted")
        sent = client.post_calls[0][1]["json"]
        self.assertEqual(sent["symbol"], "TSLA")
        self.assertEqual(sent["notional"], "100.00")
        self.assertEqual(sent["time_in_force"], "day")
        self.assertNotIn("client_order_id", result["trade_record"])
        self.assertEqual(result["trade_record"]["order_id"], "order-1")

    def test_paper_order_dry_run_validates_equity_without_posting(self):
        client = FakeAsyncClient(
            get_routes=[
                ("/account", FakeResponse({"buying_power": "5000"})),
                ("/assets/TSLA", FakeResponse({"tradable": True, "fractionable": True})),
                no_open_orders(),
            ]
        )
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="10000", position="100")
        ):
            result = async_run(
                server.alpaca_paper_order(
                    {"symbol": "TSLA", "asset_class": "equity", "status": "Staged ticket", "size": "$10000", "dry_run": True}
                )
            )
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["request"]["symbol"], "TSLA")
        self.assertEqual(result["request"]["notional"], "100.00")
        self.assertEqual(client.post_calls, [])

    def test_paper_order_blocks_wide_quant_spread(self):
        with fake_env():
            result = async_run(
                server.alpaca_paper_order(
                    {
                        "symbol": "VTAK",
                        "asset_class": "equity",
                        "status": "Staged ticket",
                        "size": "$100",
                        "spread_percent": 2.4,
                        "max_allowed_spread_percent": 1.5,
                    }
                )
            )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("wider than the Quant Scout limit", result["message"])

    def test_paper_order_dry_run_uses_quant_limit_order(self):
        client = FakeAsyncClient(
            get_routes=[
                ("/account", FakeResponse({"buying_power": "5000"})),
                ("/assets/VTAK", FakeResponse({"tradable": True, "fractionable": True})),
                no_open_orders(),
            ]
        )
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="100", position="100")
        ):
            result = async_run(
                server.alpaca_paper_order(
                    {
                        "symbol": "VTAK",
                        "asset_class": "equity",
                        "status": "Staged ticket",
                        "size": "$100 quant risk cap",
                        "suggested_order_type": "limit",
                        "limit_price": "1.22",
                        "spread_percent": 0.5,
                        "max_allowed_spread_percent": 1.5,
                        "dry_run": True,
                    }
                )
            )
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["request"]["type"], "limit")
        self.assertEqual(result["request"]["limit_price"], "1.22")
        self.assertIn("qty", result["request"])
        self.assertNotIn("notional", result["request"])

    def test_paper_order_dry_run_adds_equity_bracket_exits(self):
        client = FakeAsyncClient(
            get_routes=[
                ("/account", FakeResponse({"buying_power": "5000"})),
                ("/assets/VTAK", FakeResponse({"tradable": True, "fractionable": True})),
                no_open_orders(),
            ]
        )
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="100", position="100")
        ):
            result = async_run(
                server.alpaca_paper_order(
                    {
                        "symbol": "VTAK",
                        "asset_class": "equity",
                        "status": "Staged ticket",
                        "size": "$100 quant risk cap",
                        "suggested_order_type": "limit",
                        "limit_price": "1.22",
                        "entry": "$1.22",
                        "exit_target": "$1.25",
                        "stop": "$1.20",
                        "dry_run": True,
                    }
                )
            )
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["request"]["order_class"], "bracket")
        self.assertEqual(result["request"]["take_profit"], {"limit_price": "1.25"})
        self.assertEqual(result["request"]["stop_loss"], {"stop_price": "1.20"})
        self.assertEqual(result["request"]["qty"], "81")
        self.assertNotIn("notional", result["request"])
        self.assertEqual(result["protective_exits"]["status"], "ready")

    def test_paper_order_blocks_invalid_equity_bracket_stop(self):
        client = FakeAsyncClient(
            get_routes=[
                ("/account", FakeResponse({"buying_power": "5000"})),
                ("/assets/PENNY", FakeResponse({"tradable": True, "fractionable": True})),
            ]
        )
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="100", position="100")
        ):
            result = async_run(
                server.alpaca_paper_order(
                    {
                        "symbol": "PENNY",
                        "asset_class": "equity",
                        "status": "Staged ticket",
                        "size": "$100 quant risk cap",
                        "suggested_order_type": "limit",
                        "limit_price": "0.1200",
                        "entry": "$0.1200",
                        "exit_target": "$0.1300",
                        "stop": "$0.1190",
                    }
                )
            )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("at least $0.01 below", result["message"])
        self.assertEqual(client.post_calls, [])

    def test_paper_order_blocks_duplicate_open_order_for_same_symbol(self):
        existing_order = {
            "id": "open-vtak",
            "symbol": "VTAK",
            "status": "accepted",
            "side": "buy",
            "qty": "85",
            "filled_qty": "0",
        }
        client = FakeAsyncClient(
            get_routes=[
                ("/account", FakeResponse({"buying_power": "5000"})),
                ("/assets/VTAK", FakeResponse({"tradable": True, "fractionable": False})),
                ("/orders", FakeResponse([existing_order])),
            ]
        )
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="10000", position="100")
        ):
            result = async_run(
                server.alpaca_paper_order(
                    {
                        "symbol": "VTAK",
                        "asset_class": "equity",
                        "status": "Staged ticket",
                        "entry": "$1.17",
                        "size": "$10000 paper notional max",
                    }
                )
            )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Open Alpaca order already exists for VTAK", result["message"])
        self.assertEqual(result["existing_order"]["id"], "open-vtak")
        self.assertEqual(result["trade_record"]["quantity_or_notional"], "85 qty / filled 0/85")
        self.assertEqual(client.post_calls, [])

    def test_paper_order_dry_run_validates_fx_proxy_equity_without_posting(self):
        client = FakeAsyncClient(
            get_routes=[
                ("/account", FakeResponse({"buying_power": "5000"})),
                ("/assets/FXE", FakeResponse({"tradable": True, "fractionable": True})),
                no_open_orders(),
            ]
        )
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="50", position="100")
        ):
            result = async_run(
                server.alpaca_paper_order(
                    {"symbol": "FXE", "asset_class": "equity", "status": "Staged ticket", "size": "$50", "dry_run": True}
                )
            )
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["asset_class"], "equity")
        self.assertEqual(result["request"]["symbol"], "FXE")
        self.assertEqual(client.post_calls, [])

    def test_paper_order_submits_nonfractionable_equity_quantity(self):
        client = FakeAsyncClient(
            get_routes=[
                ("/account", FakeResponse({"buying_power": "1000"})),
                ("/assets/XYZ", FakeResponse({"tradable": True, "fractionable": False})),
                no_open_orders(),
            ],
            post_routes=[("/orders", FakeResponse({"id": "order-qty", "symbol": "XYZ", "status": "accepted", "side": "buy", "qty": "4"}))],
        )
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="100", position="100")
        ):
            result = async_run(server.alpaca_paper_order({"symbol": "XYZ", "status": "Staged ticket", "size": "$100", "entry": "$20"}))
        self.assertEqual(result["status"], "submitted")
        sent = client.post_calls[0][1]["json"]
        self.assertEqual(sent["qty"], "5")
        self.assertNotIn("notional", sent)

    def test_paper_order_submits_crypto_gtc(self):
        client = FakeAsyncClient(
            get_routes=[("/account", FakeResponse({"buying_power": "5000"})), no_open_orders()],
            post_routes=[("/orders", FakeResponse({"id": "crypto-1", "symbol": "BTC/USD", "status": "accepted", "side": "buy", "notional": "25.00"}))],
        )
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="25", position="100")
        ):
            result = async_run(server.alpaca_paper_order({"symbol": "BTC/USD", "asset_class": "crypto", "status": "Staged ticket", "size": "$25"}))
        self.assertEqual(result["status"], "submitted")
        sent = client.post_calls[0][1]["json"]
        self.assertEqual(sent["time_in_force"], "gtc")
        self.assertEqual(sent["notional"], "25.00")
        self.assertEqual(len(client.get_calls), 2)

    def test_paper_order_dry_run_validates_crypto_without_posting(self):
        client = FakeAsyncClient(get_routes=[("/account", FakeResponse({"buying_power": "5000"})), no_open_orders()])
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="25", position="100")
        ):
            result = async_run(
                server.alpaca_paper_order(
                    {"symbol": "BTC/USD", "asset_class": "crypto", "status": "Staged ticket", "size": "$25", "dry_run": True}
                )
            )
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["asset_class"], "crypto")
        self.assertEqual(result["request"]["time_in_force"], "gtc")
        self.assertNotIn("order_class", result["request"])
        self.assertEqual(client.post_calls, [])

    def test_paper_order_returns_rejection_without_client_order_id(self):
        client = FakeAsyncClient(
            get_routes=[
                ("/account", FakeResponse({"buying_power": "1000"})),
                ("/assets/TSLA", FakeResponse({"tradable": True, "fractionable": True})),
                no_open_orders(),
            ],
            post_routes=[("/orders", FakeResponse({"message": "market closed"}, status_code=422, text="market closed"))],
        )
        with fake_env(), patch.object(server.httpx, "AsyncClient", lambda *args, **kwargs: client), patch.object(
            server, "current_risk_settings", return_value=risk(order="50", position="100")
        ):
            result = async_run(server.alpaca_paper_order({"symbol": "TSLA", "status": "Staged ticket", "size": "$50"}))
        self.assertEqual(result["status"], "rejected")
        self.assertIn("HTTP 422", result["message"])
        self.assertNotIn("client_order_id", result["request"])

    def test_scout_candidate_statuses(self):
        staged = server.scout_candidate_payload(
            symbol="MOVE",
            kind="stock",
            snapshot={"latestTrade": {"p": 12}, "prevDailyBar": {"c": 10}, "dailyBar": {"v": 1_000_000}},
            asset={"tradable": True, "shortable": False, "easy_to_borrow": False},
            source="test",
            news_count=3,
            volume_ratio=5,
        )
        skipped = server.scout_candidate_payload(
            symbol="DROP",
            kind="stock",
            snapshot={"latestTrade": {"p": 8}, "prevDailyBar": {"c": 10}},
            asset={"tradable": True, "shortable": False, "easy_to_borrow": False},
            source="test",
        )
        blocked = server.scout_candidate_payload(
            symbol="HALT",
            kind="stock",
            snapshot={"latestTrade": {"p": 10}, "prevDailyBar": {"c": 9}},
            asset={"tradable": False},
            source="test",
        )
        self.assertEqual(staged["status"], "Staged ticket")
        self.assertEqual(staged["api_status"], "not_sent")
        self.assertEqual(skipped["status"], "Skipped")
        self.assertEqual(blocked["status"], "Blocked")


if __name__ == "__main__":
    unittest.main()
