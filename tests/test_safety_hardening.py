import os
import sys
import time
import unittest
import types
import json
import datetime
from types import SimpleNamespace
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class FakePlaceOrderV3Request:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeApiException(Exception):
    def __init__(self, body=""):
        super().__init__(body)
        self.body = body


fake_upstox = types.ModuleType("upstox_client")
fake_upstox.Configuration = lambda: SimpleNamespace(access_token="")
fake_upstox.ApiClient = lambda configuration: SimpleNamespace(configuration=configuration)
fake_upstox.OrderApiV3 = lambda api_client: None
fake_upstox.OrderApi = lambda api_client: None
fake_upstox.PlaceOrderV3Request = FakePlaceOrderV3Request
fake_upstox.MarketDataStreamerV3 = lambda *args, **kwargs: None
fake_rest = types.ModuleType("upstox_client.rest")
fake_rest.ApiException = FakeApiException
sys.modules.setdefault("upstox_client", fake_upstox)
sys.modules.setdefault("upstox_client.rest", fake_rest)

fake_requests = types.ModuleType("requests")
fake_requests.get = lambda *args, **kwargs: None
fake_requests.post = lambda *args, **kwargs: None
sys.modules.setdefault("requests", fake_requests)

fake_schedule = types.ModuleType("schedule")
fake_schedule.clear = lambda *args, **kwargs: None
fake_schedule.every = lambda: SimpleNamespace(day=SimpleNamespace(at=lambda _: SimpleNamespace(do=lambda *a, **k: SimpleNamespace(tag=lambda *_: None))))
fake_schedule.run_pending = lambda: None
sys.modules.setdefault("schedule", fake_schedule)

import config
import broker
import execution
import main
import state_manager
import strategy


class FakeOrderApiV3:
    def __init__(self):
        self.requests = []
        self.count = 0

    def place_order(self, body):
        self.count += 1
        order_id = f"o{self.count}"
        self.requests.append(body)
        return SimpleNamespace(data=SimpleNamespace(order_ids=[order_id]))


class FakeOrderApi:
    def __init__(self, statuses):
        self.statuses = {key: list(value) for key, value in statuses.items()}

    def get_order_status(self, order_id=None):
        data = self.statuses[order_id].pop(0)
        return SimpleNamespace(data=data)


def status(status="COMPLETE", filled_quantity=65, average_price=1.0):
    return SimpleNamespace(
        status=status,
        filled_quantity=filled_quantity,
        pending_quantity=max(0, 65 - filled_quantity),
        average_price=average_price,
        status_message="",
        status_message_raw="",
    )


class SafetyHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp_name = os.path.join(ROOT, ".test_tmp_state")
        os.makedirs(self.tmp_name, exist_ok=True)
        self.old_state_file = state_manager.STATE_FILE
        self.old_env = config.ENVIRONMENT
        self.old_qty = config.get_nifty_qty
        self.old_expiries_file = config.EXPIRIES_FILE
        self.old_strategy_base = strategy.BASE_DIR
        self.current_state_file = os.path.join(self.tmp_name, "trade_state.json")
        if os.path.exists(self.current_state_file):
            os.remove(self.current_state_file)
        state_manager.STATE_FILE = self.current_state_file
        state_manager._cached_state = None
        state_manager._last_mtime = 0.0
        config.ENVIRONMENT = "LIVE"
        config.get_nifty_qty = lambda: 65
        config.EXPIRIES_FILE = os.path.join(self.tmp_name, "expiries.json")
        strategy.BASE_DIR = self.tmp_name
        broker.set_broker_for_tests(None)

    def tearDown(self):
        try:
            if os.path.exists(self.current_state_file):
                os.remove(self.current_state_file)
            os.rmdir(self.tmp_name)
        except OSError:
            pass
        state_manager.STATE_FILE = self.old_state_file
        state_manager._cached_state = None
        state_manager._last_mtime = 0.0
        config.ENVIRONMENT = self.old_env
        config.get_nifty_qty = self.old_qty
        config.EXPIRIES_FILE = self.old_expiries_file
        strategy.BASE_DIR = self.old_strategy_base
        broker.set_broker_for_tests(None)

    def test_confirmed_entry_saves_actual_average_prices(self):
        v3 = FakeOrderApiV3()
        order_api = FakeOrderApi({
            "o1": [status(average_price=5.0)],
            "o2": [status(average_price=4.0)],
            "o3": [status(average_price=20.0)],
            "o4": [status(average_price=21.0)],
        })
        legs = {"buy_ce": "BCE", "buy_pe": "BPE", "sell_ce": "SCE", "sell_pe": "SPE"}
        prices = {"buy_ce": 6.0, "buy_pe": 6.0, "sell_ce": 18.0, "sell_pe": 18.0}
        strikes = {"buy_ce": 110, "buy_pe": 90, "sell_ce": 100, "sell_pe": 100}

        with patch.object(execution, "_make_order_apis", return_value=(v3, order_api)), \
             patch.object(execution, "send_telegram_alert"), \
             patch.object(execution, "log_trade"):
            self.assertTrue(execution.place_iron_butterfly_basket(legs, "NIFTY", prices, strikes))

        saved = state_manager.load_state()
        self.assertEqual(saved["entry_prices"], {"buy_ce": 5.0, "buy_pe": 4.0, "sell_ce": 20.0, "sell_pe": 21.0})
        self.assertEqual(len(saved["execution_info"]["fills"]), 4)

    def test_partial_entry_rolls_back_and_does_not_save_success_state(self):
        v3 = FakeOrderApiV3()
        order_api = FakeOrderApi({
            "o1": [status(filled_quantity=10, average_price=5.0)],
            "o2": [status(filled_quantity=10, average_price=5.0)],
        })
        legs = {"buy_ce": "BCE", "buy_pe": "BPE", "sell_ce": "SCE", "sell_pe": "SPE"}
        prices = {"buy_ce": 6.0, "buy_pe": 6.0, "sell_ce": 18.0, "sell_pe": 18.0}
        strikes = {"buy_ce": 110, "buy_pe": 90, "sell_ce": 100, "sell_pe": 100}

        with patch.object(execution, "_make_order_apis", return_value=(v3, order_api)), \
             patch.object(execution, "send_telegram_alert"), \
             patch.object(execution, "log_trade"):
            self.assertFalse(execution.place_iron_butterfly_basket(legs, "NIFTY", prices, strikes))

        self.assertIsNone(state_manager.load_state())
        self.assertEqual([req.instrument_token for req in v3.requests], ["BCE", "BCE"])
        self.assertEqual(v3.requests[-1].quantity, 10)

    def test_rollback_only_reverses_confirmed_fills(self):
        v3 = FakeOrderApiV3()
        order_api = FakeOrderApi({
            "o1": [status(average_price=5.0)],
            "o2": [status(status="REJECTED", filled_quantity=0, average_price=0.0)],
            "o3": [status(average_price=5.0)],
        })
        legs = {"buy_ce": "BCE", "buy_pe": "BPE", "sell_ce": "SCE", "sell_pe": "SPE"}
        prices = {"buy_ce": 6.0, "buy_pe": 6.0, "sell_ce": 18.0, "sell_pe": 18.0}
        strikes = {"buy_ce": 110, "buy_pe": 90, "sell_ce": 100, "sell_pe": 100}

        with patch.object(execution, "_make_order_apis", return_value=(v3, order_api)), \
             patch.object(execution, "send_telegram_alert"), \
             patch.object(execution, "log_trade"):
            self.assertFalse(execution.place_iron_butterfly_basket(legs, "NIFTY", prices, strikes))

        self.assertEqual([req.instrument_token for req in v3.requests], ["BCE", "BPE", "BCE"])

    def test_exit_keeps_hedge_when_short_close_fails(self):
        state_manager.save_state(
            "NIFTY",
            {"buy_ce": "BCE", "buy_pe": "BPE", "sell_ce": "SCE", "sell_pe": "SPE"},
            {"buy_ce": 5.0, "buy_pe": 5.0, "sell_ce": 20.0, "sell_pe": 20.0},
            65,
            {"buy_ce": 110, "buy_pe": 90, "sell_ce": 100, "sell_pe": 100},
        )
        v3 = FakeOrderApiV3()
        order_api = FakeOrderApi({
            "o1": [status(status="REJECTED", filled_quantity=0, average_price=0.0)],
            "o2": [status(average_price=10.0)],
            "o3": [status(average_price=4.0)],
        })
        with patch.object(execution, "_make_order_apis", return_value=(v3, order_api)), \
             patch.object(execution, "send_telegram_alert"), \
             patch.object(execution, "log_trade"):
            execution.square_off_all({"buy_ce": 5.0, "buy_pe": 4.0, "sell_ce": 22.0, "sell_pe": 10.0})

        self.assertNotIn("BCE", [req.instrument_token for req in v3.requests])
        self.assertIsNotNone(state_manager.load_state())

    def test_socket_dead_reconnects_same_active_trade(self):
        calls = []

        def fake_monitor(legs, callback):
            calls.append(legs)
            return ("SOCKET_DEAD", {}) if len(calls) == 1 else ("TAKE_PROFIT", {"sell_ce": 1})

        with patch.object(main, "monitor_live_prices", side_effect=fake_monitor), \
             patch.object(main.state_manager, "load_state", return_value={"active": True}), \
             patch.object(main.state_manager, "update_state"), \
             patch.object(main, "write_heartbeat"), \
             patch.object(main.time, "sleep"):
            result = main.monitor_with_reconnects({"sell_ce": "SCE"}, "NIFTY")

        self.assertEqual(result[0], "TAKE_PROFIT")
        self.assertEqual(len(calls), 2)

    def test_stale_ticks_block_risk_decision(self):
        state_manager.save_state(
            "NIFTY",
            {"buy_ce": "BCE", "buy_pe": "BPE", "sell_ce": "SCE", "sell_pe": "SPE"},
            {"buy_ce": 5.0, "buy_pe": 5.0, "sell_ce": 20.0, "sell_pe": 20.0},
            65,
            {"buy_ce": 110, "buy_pe": 90, "sell_ce": 100, "sell_pe": 100},
        )
        old_ts = time.time() - config.MAX_FEED_STALENESS_SECONDS - 10
        live_data = {token: {"ltp": 10.0, "ts": old_ts} for token in ("BCE", "BPE", "SCE", "SPE")}
        with self.assertRaises(ValueError):
            strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

    def test_atm_selection_uses_nearest_available_strike(self):
        chain = []
        for strike in (100, 150, 200):
            chain.append({
                "strike_price": strike,
                "call_options": {"instrument_key": f"C{strike}", "market_data": {"ltp": 20 if strike == 150 else 5}, "greeks": {"delta": 0.5}},
                "put_options": {"instrument_key": f"P{strike}", "market_data": {"ltp": 20 if strike == 150 else 5}, "greeks": {"delta": -0.5}},
            })
        legs, prices, strikes = strategy.calculate_iron_butterfly_legs("NIFTY", 126, chain, wing_delta=50)
        self.assertEqual(strikes["sell_ce"], 150)
        self.assertEqual(legs["sell_ce"], "C150")

    def test_calendar_selects_next_weekly_expiry_from_manual_json(self):
        with open(config.EXPIRIES_FILE, "w") as f:
            json.dump({
                "NIFTY": ["2026-04-23", "2026-04-30", "2026-05-07"],
                "SENSEX": ["2026-04-24", "2026-05-01", "2026-05-08"],
                "HOLIDAYS": ["2026-05-01"],
            }, f)

        now = datetime.datetime(2026, 4, 29, 10, 0)
        self.assertEqual(config.get_next_expiry("NIFTY", now=now), "2026-04-30")
        self.assertEqual(config.get_next_expiry("SENSEX", now=now), "2026-05-08")
        self.assertEqual(config.validate_expiry_calendar(now=now), [])

    def test_manual_exit_uses_fresh_broker_quotes_for_pnl(self):
        class FreshQuoteBroker:
            def get_fresh_option_quotes(self, instrument_keys):
                return {"SCE": 18.0, "SPE": 17.0, "BCE": 4.0, "BPE": 4.0}

        broker.set_broker_for_tests(FreshQuoteBroker())
        config.ENVIRONMENT = "SANDBOX"
        state_manager.save_state(
            "NIFTY",
            {"buy_ce": "BCE", "buy_pe": "BPE", "sell_ce": "SCE", "sell_pe": "SPE"},
            {"buy_ce": 5.0, "buy_pe": 5.0, "sell_ce": 20.0, "sell_pe": 20.0},
            65,
            {"buy_ce": 110, "buy_pe": 90, "sell_ce": 100, "sell_pe": 100},
        )

        with patch.object(execution, "send_telegram_alert"), patch.object(execution, "log_trade") as log_trade:
            execution.square_off_all({"buy_ce": 5.0, "buy_pe": 5.0, "sell_ce": 20.0, "sell_pe": 20.0}, exit_reason="MANUAL_EXIT")

        args, kwargs = log_trade.call_args
        self.assertEqual(args[0], "EXIT")
        self.assertEqual(args[5], "Local Paper Trade Closed (Fresh broker quote snapshot)")
        self.assertEqual(args[4], 195.0)
        self.assertEqual(kwargs["exit_reason"], "MANUAL_EXIT")

    def test_profit_lock_floor_gets_short_atm_grace_before_exit(self):
        class MarketHoursDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 4, 29, 10, 30)

        state_manager.save_state(
            "NIFTY",
            {"buy_ce": "BCE", "buy_pe": "BPE", "sell_ce": "SCE", "sell_pe": "SPE"},
            {"buy_ce": 5.0, "buy_pe": 5.0, "sell_ce": 20.0, "sell_pe": 20.0},
            65,
            {"buy_ce": 110, "buy_pe": 90, "sell_ce": 100, "sell_pe": 100},
        )
        state_manager.update_many({
            "applied_target_pct": 10,
            "profit_lock_tier": 2,
            "profit_lock_floor": 0.035,
        })
        live_data = {
            "SCE": {"ltp": 19.0, "ts": time.time()},
            "SPE": {"ltp": 18.2, "ts": time.time()},
            "BCE": {"ltp": 4.0, "ts": time.time()},
            "BPE": {"ltp": 4.0, "ts": time.time()},
            "NSE_INDEX|Nifty 50": {"ltp": 100.0, "ts": time.time()},
            "NSE_INDEX|India VIX": {"ltp": 15.0, "ts": time.time()},
        }

        with patch.object(strategy.datetime, "datetime", MarketHoursDatetime):
            result, _ = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])
        self.assertFalse(result)

        old_grace = time.time() - config.PROFIT_LOCK_ATM_GRACE_SECONDS - 1
        state_manager.update_state("profit_lock_grace_started_ts", old_grace)
        with patch.object(strategy.datetime, "datetime", MarketHoursDatetime):
            result, _ = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])
        self.assertEqual(result, "TAKE_PROFIT")


if __name__ == "__main__":
    unittest.main()
