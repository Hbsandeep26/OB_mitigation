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
import data_feed
import execution
import main
import state_manager
import strategy
import btst_vix_router
import eod_engine
import market_context
import position_sizing


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
        self.old_targets_enabled = config.SNIPER_TARGETS_ENABLED
        self.old_drift_threshold = config.ATM_DRIFT_EJECT_THRESHOLD
        self.old_condor_drift_threshold = config.CONDOR_ATM_DRIFT_THRESHOLD
        self.old_vix_toggle_level = config.INDIA_VIX_TOGGLE_LEVEL
        self.old_condor_short_offset = config.CONDOR_SHORT_STRIKE_OFFSET
        self.old_btst_spread_width = config.BTST_SPREAD_WIDTH_POINTS
        self.old_btst_momentum_enabled = config.BTST_MOMENTUM_ENABLED
        self.old_btst_recenter_min_drift = config.BTST_RECENTER_MIN_DRIFT_RATIO
        self.old_virtual_capital = config.VIRTUAL_CAPITAL
        self.old_max_capital_utilization = config.MAX_CAPITAL_UTILIZATION
        self.old_post_emergency_enabled = config.POST_EMERGENCY_REENTRY_ENABLED
        self.old_post_emergency_cooldown = config.POST_EMERGENCY_REENTRY_COOLDOWN_SECONDS
        self.old_catastrophe_multiplier = config.SNIPER_CATASTROPHE_MULTIPLIER
        self.old_exit_confirmation_enabled = config.EMERGENCY_EXIT_CONFIRMATION_ENABLED
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
        config.SNIPER_TARGETS_ENABLED = True
        config.ATM_DRIFT_EJECT_THRESHOLD = 0.20
        config.SNIPER_DRIFT_EJECT_RATIO = config.ATM_DRIFT_EJECT_THRESHOLD
        config.CONDOR_ATM_DRIFT_THRESHOLD = 0.40
        config.Condor_ATM_Drift_Threshold = config.CONDOR_ATM_DRIFT_THRESHOLD
        config.INDIA_VIX_TOGGLE_LEVEL = 15.0
        config.CONDOR_SHORT_STRIKE_OFFSET = 300.0
        config.BTST_SPREAD_WIDTH_POINTS = 400.0
        config.BTST_MOMENTUM_ENABLED = True
        config.BTST_RECENTER_MIN_DRIFT_RATIO = 0.06
        config.VIRTUAL_CAPITAL = 220000.0
        config.MAX_CAPITAL_UTILIZATION = 0.80
        config.POST_EMERGENCY_REENTRY_ENABLED = True
        config.POST_EMERGENCY_REENTRY_COOLDOWN_SECONDS = 0.0
        config.SNIPER_CATASTROPHE_MULTIPLIER = 1.05
        config.EMERGENCY_EXIT_CONFIRMATION_ENABLED = False
        strategy.BASE_DIR = self.tmp_name
        broker.set_broker_for_tests(None)

    def tearDown(self):
        try:
            for filename in ("btst_flag.txt", "manual_exit_flag.txt", "graceful_stop_flag.txt", "expiries.json"):
                flag_path = os.path.join(self.tmp_name, filename)
                if os.path.exists(flag_path):
                    os.remove(flag_path)
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
        config.SNIPER_TARGETS_ENABLED = self.old_targets_enabled
        config.ATM_DRIFT_EJECT_THRESHOLD = self.old_drift_threshold
        config.SNIPER_DRIFT_EJECT_RATIO = self.old_drift_threshold
        config.CONDOR_ATM_DRIFT_THRESHOLD = self.old_condor_drift_threshold
        config.Condor_ATM_Drift_Threshold = self.old_condor_drift_threshold
        config.INDIA_VIX_TOGGLE_LEVEL = self.old_vix_toggle_level
        config.CONDOR_SHORT_STRIKE_OFFSET = self.old_condor_short_offset
        config.BTST_SPREAD_WIDTH_POINTS = self.old_btst_spread_width
        config.BTST_MOMENTUM_ENABLED = self.old_btst_momentum_enabled
        config.BTST_RECENTER_MIN_DRIFT_RATIO = self.old_btst_recenter_min_drift
        config.VIRTUAL_CAPITAL = self.old_virtual_capital
        config.MAX_CAPITAL_UTILIZATION = self.old_max_capital_utilization
        config.POST_EMERGENCY_REENTRY_ENABLED = self.old_post_emergency_enabled
        config.POST_EMERGENCY_REENTRY_COOLDOWN_SECONDS = self.old_post_emergency_cooldown
        config.SNIPER_CATASTROPHE_MULTIPLIER = self.old_catastrophe_multiplier
        config.EMERGENCY_EXIT_CONFIRMATION_ENABLED = self.old_exit_confirmation_enabled
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
            return ("SOCKET_DEAD", {}) if len(calls) == 1 else ("SNIPER_TARGET", {"sell_ce": 1})

        with patch.object(main, "monitor_live_prices", side_effect=fake_monitor), \
             patch.object(main.state_manager, "load_state", return_value={"active": True}), \
             patch.object(main.state_manager, "update_state"), \
             patch.object(main, "write_heartbeat"), \
             patch.object(main.time, "sleep"):
            result = main.monitor_with_reconnects({"sell_ce": "SCE"}, "NIFTY")

        self.assertEqual(result[0], "SNIPER_TARGET")
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

    def test_manual_exit_falls_back_to_live_ledger_snapshot_for_pnl(self):
        config.ENVIRONMENT = "SANDBOX"
        self._save_sniper_state()
        manual_exit_file = os.path.join(strategy.BASE_DIR, "manual_exit_flag.txt")
        with open(manual_exit_file, "w") as f:
            f.write("TRUE")

        live_data = self._sniper_live_data(18.0, 17.0, 4.0, 4.0, spot=100.0)

        result, exit_prices = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])
        self.assertEqual(result, "MANUAL_EXIT")

        with patch.object(execution, "send_telegram_alert"), patch.object(execution, "log_trade") as log_trade:
            execution.square_off_all(exit_prices, exit_reason=result)

        args, kwargs = log_trade.call_args
        self.assertEqual(args[0], "EXIT")
        self.assertEqual(args[3], 27.0)
        self.assertEqual(args[4], 195.0)
        self.assertEqual(kwargs["exit_reason"], "MANUAL_EXIT")

    def test_atm_drift_exit_logs_trigger_tick_prices_without_rest_override(self):
        class BadFreshQuoteBroker:
            def get_fresh_option_quotes(self, instrument_keys):
                return {"SCE": 99.0, "SPE": 99.0, "BCE": 1.0, "BPE": 1.0}

        broker.set_broker_for_tests(BadFreshQuoteBroker())
        config.ENVIRONMENT = "SANDBOX"
        self._save_sniper_state()
        live_data = self._sniper_live_data(17.0, 17.0, 3.8, 3.8, spot=121.0)

        with self._market_time_patch():
            result, exit_prices = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])
        self.assertEqual(result, "ATM_DRIFT")

        with patch.object(execution, "send_telegram_alert"), patch.object(execution, "log_trade") as log_trade:
            execution.square_off_all(exit_prices, exit_reason=result)

        args, kwargs = log_trade.call_args
        self.assertEqual(args[2], {"sell_ce": 17.0, "sell_pe": 17.0, "buy_ce": 3.8, "buy_pe": 3.8})
        self.assertEqual(args[3], 26.4)
        self.assertEqual(kwargs["exit_reason"], "ATM_DRIFT")

    def _save_sniper_state(self, sniper_state="INITIAL"):
        state_manager.save_state(
            "NIFTY",
            {"buy_ce": "BCE", "buy_pe": "BPE", "sell_ce": "SCE", "sell_pe": "SPE"},
            {"buy_ce": 5.0, "buy_pe": 5.0, "sell_ce": 20.0, "sell_pe": 20.0},
            65,
            {"buy_ce": 150, "buy_pe": 50, "sell_ce": 100, "sell_pe": 100},
        )
        state_manager.update_many({
            "sniper_state": sniper_state,
            "entry_net_premium": 30.0,
        })

    def _sniper_live_data(self, sell_ce, sell_pe, buy_ce, buy_pe, spot=100.0):
        now = time.time()
        return {
            "SCE": {"ltp": sell_ce, "ts": now},
            "SPE": {"ltp": sell_pe, "ts": now},
            "BCE": {"ltp": buy_ce, "ts": now},
            "BPE": {"ltp": buy_pe, "ts": now},
            "NSE_INDEX|Nifty 50": {"ltp": spot, "ts": now},
        }

    def _option_chain_for_strikes(self, strike_prices):
        chain = []
        for strike, prices in strike_prices.items():
            call_ltp, put_ltp = prices
            chain.append({
                "strike_price": strike,
                "call_options": {
                    "instrument_key": f"C{strike}",
                    "market_data": {"ltp": call_ltp},
                    "greeks": {"delta": 0.5},
                },
                "put_options": {
                    "instrument_key": f"P{strike}",
                    "market_data": {"ltp": put_ltp},
                    "greeks": {"delta": -0.5},
                },
            })
        return chain

    def _market_time_patch(self, hour=10, minute=30):
        class MarketHoursDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 4, 29, hour, minute)

        return patch.object(strategy.datetime, "datetime", MarketHoursDatetime)

    def test_default_wide_wing_selection_uses_configured_five_delta(self):
        old_delta = config.SNIPER_WING_DELTA
        config.SNIPER_WING_DELTA = 5.0
        try:
            chain = []
            for strike, ce_delta, pe_delta in (
                (50, 0.95, -0.05),
                (75, 0.75, -0.20),
                (100, 0.50, -0.50),
                (125, 0.20, -0.75),
                (150, 0.05, -0.95),
            ):
                chain.append({
                    "strike_price": strike,
                    "call_options": {
                        "instrument_key": f"C{strike}",
                        "market_data": {"ltp": 20 if strike == 100 else 3},
                        "greeks": {"delta": ce_delta},
                    },
                    "put_options": {
                        "instrument_key": f"P{strike}",
                        "market_data": {"ltp": 20 if strike == 100 else 3},
                        "greeks": {"delta": pe_delta},
                    },
                })

            legs, _, strikes = strategy.calculate_iron_butterfly_legs("NIFTY", 101, chain)
        finally:
            config.SNIPER_WING_DELTA = old_delta

        self.assertEqual(legs["buy_ce"], "C150")
        self.assertEqual(legs["buy_pe"], "P50")
        self.assertEqual(strikes["sell_ce"], 100)

    def test_wing_selection_falls_back_to_farthest_valid_otm_when_greeks_missing(self):
        chain = []
        for strike in (50, 75, 100, 125, 150):
            chain.append({
                "strike_price": strike,
                "call_options": {"instrument_key": f"C{strike}", "market_data": {"ltp": 20 if strike == 100 else 2}, "greeks": {}},
                "put_options": {"instrument_key": f"P{strike}", "market_data": {"ltp": 20 if strike == 100 else 2}, "greeks": {}},
            })

        legs, _, _ = strategy.calculate_iron_butterfly_legs("NIFTY", 100, chain)

        self.assertEqual(legs["buy_ce"], "C150")
        self.assertEqual(legs["buy_pe"], "P50")

    def test_vix_router_keeps_butterfly_when_vix_is_below_threshold(self):
        calls = []

        def fake_butterfly(index_symbol, spot, chain, buy_leg_percent=None):
            calls.append((index_symbol, spot, buy_leg_percent))
            return (
                {"sell_ce": "SCE", "sell_pe": "SPE", "buy_ce": "BCE", "buy_pe": "BPE"},
                {"sell_ce": 20.0, "sell_pe": 20.0, "buy_ce": 5.0, "buy_pe": 5.0},
                {"sell_ce": 100, "sell_pe": 100, "buy_ce": 150, "buy_pe": 50},
            )

        route = btst_vix_router.route_intraday_neutral_strategy("NIFTY", 100.0, [], 14.99, fake_butterfly)

        self.assertEqual(route.strategy_type, btst_vix_router.STRATEGY_IRON_BUTTERFLY)
        self.assertEqual(route.drift_threshold, config.ATM_DRIFT_EJECT_THRESHOLD)
        self.assertEqual(calls, [("NIFTY", 100.0, config.BUY_LEG_PERCENT)])

    def test_vix_router_builds_condor_above_threshold_with_premium_wings(self):
        chain = self._option_chain_for_strikes({
            19500: (600.0, 5.0),
            19600: (500.0, 10.0),
            19700: (400.0, 100.0),
            20000: (250.0, 250.0),
            20300: (100.0, 400.0),
            20400: (10.0, 500.0),
            20500: (5.0, 600.0),
        })

        route = btst_vix_router.route_intraday_neutral_strategy(
            "NIFTY",
            20000.0,
            chain,
            16.0,
            lambda *args, **kwargs: self.fail("Butterfly calculator should not be used in high VIX"),
        )

        self.assertEqual(route.strategy_type, btst_vix_router.STRATEGY_IRON_CONDOR)
        self.assertEqual(route.strikes["sell_ce"], 20300.0)
        self.assertEqual(route.strikes["sell_pe"], 19700.0)
        self.assertEqual(route.strikes["buy_ce"], 20500.0)
        self.assertEqual(route.strikes["buy_pe"], 19500.0)
        self.assertEqual(route.drift_threshold, config.CONDOR_ATM_DRIFT_THRESHOLD)

    def test_condor_rejects_when_long_wings_cannot_clear_short_strikes(self):
        chain = self._option_chain_for_strikes({
            19700: (400.0, 100.0),
            20000: (250.0, 250.0),
            20300: (100.0, 400.0),
        })

        legs, prices, strikes = btst_vix_router.calculate_iron_condor_legs("NIFTY", 20000.0, chain)

        self.assertIsNone(legs)
        self.assertIsNone(prices)
        self.assertIsNone(strikes)

    def test_condor_uses_distinct_wider_atm_drift_threshold(self):
        state_manager.save_state(
            "NIFTY",
            {"buy_ce": "BCE", "buy_pe": "BPE", "sell_ce": "SCE", "sell_pe": "SPE"},
            {"buy_ce": 5.0, "buy_pe": 5.0, "sell_ce": 20.0, "sell_pe": 20.0},
            65,
            {"buy_ce": 200, "buy_pe": 0, "sell_ce": 130, "sell_pe": 70, "atm": 100},
        )
        state_manager.update_many({
            "strategy_type": btst_vix_router.STRATEGY_IRON_CONDOR,
            "entry_net_premium": 30.0,
        })

        with self._market_time_patch():
            result, _ = strategy.risk_management_evaluator(
                self._sniper_live_data(20.0, 20.0, 5.0, 5.0, spot=130.0),
                state_manager.load_state()["legs"],
            )
        self.assertFalse(result)

        with self._market_time_patch():
            result, _ = strategy.risk_management_evaluator(
                self._sniper_live_data(20.0, 20.0, 5.0, 5.0, spot=145.0),
                state_manager.load_state()["legs"],
            )
        self.assertEqual(result, "ATM_DRIFT")

    def test_btst_momentum_neutral_zone_aborts_trade(self):
        signal = btst_vix_router.evaluate_btst_momentum_signal(
            current_price=50.0,
            ema_15m_20=49.0,
            daily_low=0.0,
            daily_high=100.0,
        )

        self.assertEqual(signal.signal, btst_vix_router.SIGNAL_NEUTRAL)

    def test_btst_bullish_high_vix_routes_to_bull_put_credit_buy_first(self):
        chain = self._option_chain_for_strikes({
            19200: (450.0, 10.0),
            19600: (400.0, 25.0),
            20000: (150.0, 150.0),
            20400: (25.0, 400.0),
        })

        route = btst_vix_router.route_btst_momentum_strategy(
            "NIFTY",
            20000.0,
            chain,
            16.0,
            ema_15m_20=19900.0,
            daily_low=19000.0,
            daily_high=20000.0,
        )

        self.assertEqual(route.strategy_type, btst_vix_router.STRATEGY_BTST_BULL_PUT_CREDIT)
        self.assertEqual(route.legs, {"buy_pe": "P19200", "sell_pe": "P19600"})
        self.assertLess(route.strikes["sell_pe"], route.strikes["atm"])
        self.assertLess(route.strikes["buy_pe"], route.strikes["sell_pe"])
        self.assertEqual(route.order_sequence, [("buy_pe", "BUY"), ("sell_pe", "SELL")])

    def test_btst_bearish_low_vix_routes_to_bear_put_debit_buy_first(self):
        chain = self._option_chain_for_strikes({
            19600: (400.0, 25.0),
            20000: (150.0, 150.0),
            20400: (25.0, 400.0),
        })

        route = btst_vix_router.route_btst_momentum_strategy(
            "NIFTY",
            20000.0,
            chain,
            14.0,
            ema_15m_20=20100.0,
            daily_low=20000.0,
            daily_high=21000.0,
        )

        self.assertEqual(route.strategy_type, btst_vix_router.STRATEGY_BTST_BEAR_PUT_DEBIT)
        self.assertEqual(route.legs, {"buy_pe": "P20000", "sell_pe": "P19600"})
        self.assertEqual(route.order_sequence, [("buy_pe", "BUY"), ("sell_pe", "SELL")])

    def test_catastrophe_kill_overrides_drift_and_profit_logic(self):
        self._save_sniper_state()
        live_data = self._sniper_live_data(23.0, 23.0, 5.0, 5.0, spot=100.0)

        with self._market_time_patch():
            result, _ = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

        self.assertEqual(result, "CATASTROPHE_KILL")

    def test_drift_ejector_exits_before_profit_logic(self):
        self._save_sniper_state()
        live_data = self._sniper_live_data(17.0, 17.0, 3.8, 3.8, spot=117.5)

        with self._market_time_patch():
            result, _ = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

        self.assertEqual(result, "ATM_DRIFT")

    def test_sniper_target_exits_when_market_is_not_pinned(self):
        self._save_sniper_state()
        live_data = self._sniper_live_data(17.0, 17.0, 3.8, 3.8, spot=109.0)

        with self._market_time_patch():
            result, _ = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

        self.assertEqual(result, "SNIPER_TARGET")

    def test_sniper_target_exits_even_when_market_is_pinned(self):
        self._save_sniper_state()
        live_data = self._sniper_live_data(17.0, 17.0, 3.8, 3.8, spot=105.0)

        with self._market_time_patch():
            result, _ = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

        self.assertEqual(result, "SNIPER_TARGET")
        self.assertEqual(state_manager.load_state()["sniper_state"], "INITIAL")

    def test_standard_profit_target_can_be_disabled(self):
        config.SNIPER_TARGETS_ENABLED = False
        self._save_sniper_state()
        live_data = self._sniper_live_data(17.0, 17.0, 3.8, 3.8, spot=100.0)

        with self._market_time_patch():
            result, _ = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

        self.assertFalse(result)

    def test_old_leg_stop_loss_no_longer_triggers_strategy_exit(self):
        self._save_sniper_state()
        live_data = self._sniper_live_data(45.0, 5.0, 10.0, 10.0, spot=100.0)

        with self._market_time_patch():
            result, _ = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

        self.assertFalse(result)

    def test_eod_missing_context_squares_off_neutral(self):
        self._save_sniper_state()
        btst_file = os.path.join(strategy.BASE_DIR, "btst_flag.txt")
        with open(btst_file, "w") as f:
            f.write("TRUE")
        live_data = self._sniper_live_data(20.0, 20.0, 5.0, 5.0, spot=102.0)

        with self._market_time_patch(15, 25):
            result, _ = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

        self.assertEqual(result, "EOD_SQUARE_OFF")

    def test_eod_neutral_context_carries_forward(self):
        class NeutralContextBroker:
            def get_option_chain(self, index_symbol, expiry_date):
                return [
                    {
                        "strike_price": 100,
                        "underlying_spot_price": 100.0,
                        "call_options": {"market_data": {"oi": 100}},
                        "put_options": {"market_data": {"oi": 90}},
                    }
                ]

            def get_india_vix(self):
                return 12.0

            def get_spot_price(self, index_symbol):
                return 100.0

        broker.set_broker_for_tests(NeutralContextBroker())
        self._save_sniper_state()
        btst_file = os.path.join(strategy.BASE_DIR, "btst_flag.txt")
        with open(btst_file, "w") as f:
            f.write("TRUE")
        live_data = self._sniper_live_data(18.0, 18.0, 4.5, 4.5, spot=100.0)

        with self._market_time_patch(15, 25):
            result, _ = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

        self.assertEqual(result, "EOD_CARRY")

    def test_post_emergency_reentry_blocks_near_sensex_cutoff(self):
        class NearCutoffDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 4, 29, 15, 10)

        with patch.object(main, "datetime", NearCutoffDatetime):
            allowed = main.post_emergency_reentry_allowed(
                "SENSEX",
                {"buy_ce": "BCE", "buy_pe": "BPE", "sell_ce": "SCE", "sell_pe": "SPE"},
                {"sell_ce": 17.0, "sell_pe": 17.0, "buy_ce": 3.8, "buy_pe": 3.8},
                "CATASTROPHE_KILL",
                15,
                15,
                reference_spot=77000.0,
            )

        self.assertFalse(allowed)

    def test_post_emergency_reentry_allows_stable_premium_and_spot(self):
        class MidSessionDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 4, 29, 12, 0)

        legs = {"buy_ce": "BCE", "buy_pe": "BPE", "sell_ce": "SCE", "sell_pe": "SPE"}
        with patch.object(main, "datetime", MidSessionDatetime), \
             patch.object(main, "get_fresh_option_quotes", return_value={
                 "SCE": 17.1, "SPE": 16.9, "BCE": 3.8, "BPE": 3.8,
             }), \
             patch.object(main, "get_spot_price", return_value=100.05):
            allowed = main.post_emergency_reentry_allowed(
                "NIFTY",
                legs,
                {"sell_ce": 17.0, "sell_pe": 17.0, "buy_ce": 3.8, "buy_pe": 3.8},
                "ATM_DRIFT",
                15,
                25,
                reference_spot=100.0,
            )

        self.assertTrue(allowed)

    def test_unconfirmed_catastrophe_is_ignored_when_broker_quotes_disagree(self):
        class StableBroker:
            def get_fresh_option_quotes(self, instrument_keys):
                return {"SCE": 20.0, "SPE": 20.0, "BCE": 5.0, "BPE": 5.0}

        config.EMERGENCY_EXIT_CONFIRMATION_ENABLED = True
        broker.set_broker_for_tests(StableBroker())
        self._save_sniper_state()
        live_data = self._sniper_live_data(23.0, 23.0, 5.0, 5.0, spot=100.0)

        with self._market_time_patch():
            result, prices = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

        self.assertFalse(result)
        self.assertEqual(prices, {})
        self.assertEqual(state_manager.load_state()["feed_status"], "UNCONFIRMED_CATASTROPHE:REST_DISAGREE")

    def test_confirmed_catastrophe_uses_broker_snapshot_prices(self):
        class PanicBroker:
            def get_fresh_option_quotes(self, instrument_keys):
                return {"SCE": 24.0, "SPE": 24.0, "BCE": 5.0, "BPE": 5.0}

        config.EMERGENCY_EXIT_CONFIRMATION_ENABLED = True
        broker.set_broker_for_tests(PanicBroker())
        self._save_sniper_state()
        live_data = self._sniper_live_data(23.0, 23.0, 5.0, 5.0, spot=100.0)

        with self._market_time_patch():
            result, prices = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

        self.assertEqual(result, "CATASTROPHE_KILL")
        self.assertEqual(prices, {"sell_ce": 24.0, "sell_pe": 24.0, "buy_ce": 5.0, "buy_pe": 5.0})

    def test_unconfirmed_atm_drift_is_ignored_when_broker_spot_disagrees(self):
        class StableSpotBroker:
            def get_spot_price(self, index_symbol):
                return 100.0

        config.EMERGENCY_EXIT_CONFIRMATION_ENABLED = True
        broker.set_broker_for_tests(StableSpotBroker())
        self._save_sniper_state()
        live_data = self._sniper_live_data(17.0, 17.0, 3.8, 3.8, spot=121.0)

        with self._market_time_patch():
            result, prices = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

        self.assertFalse(result)
        self.assertEqual(prices, {})
        self.assertEqual(state_manager.load_state()["feed_status"], "UNCONFIRMED_ATM_DRIFT:REST_DISAGREE")

    def test_manual_entry_after_nifty_expiry_afternoon_resolves_to_sensex(self):
        now_dt = datetime.datetime(2026, 5, 12, 12, 38)

        session = main.session_for_time(now_dt, "2026-05-12", "2026-05-14")

        self.assertEqual(session, ("SENSEX", "2026-05-14", 15, 25))

    def test_stream_tick_uses_exchange_ltt_when_available(self):
        tick = data_feed._extract_ltp_tick(
            {"fullFeed": {"marketFF": {"ltpc": {"ltp": 123.45, "ltt": "1778567400000"}}}},
            received_at=1778567500.0,
        )

        self.assertEqual(tick["ltp"], 123.45)
        self.assertEqual(tick["ts"], 1778567400.0)
        self.assertEqual(tick["received_ts"], 1778567500.0)

    def test_sandbox_position_sizing_replaces_manual_quantity(self):
        approved = position_sizing.calculate_position_size(
            "SANDBOX", "IRON_BUTTERFLY", 14.0, 300000.0, "NIFTY", 65
        )
        rejected = position_sizing.calculate_position_size(
            "SANDBOX", "IRON_BUTTERFLY", 14.0, 220000.0, "NIFTY", 65
        )

        self.assertEqual(approved["status"], "APPROVED")
        self.assertEqual(approved["lots_to_deploy"], 1)
        self.assertEqual(approved["quantity"], 65)
        self.assertEqual(rejected["status"], "REJECTED")

    def test_live_position_sizing_prefers_basket_margin(self):
        class MarginBroker:
            def get_available_margin(self):
                return 500000.0, {"source": "TEST_FUNDS"}

            def get_order_margin(self, instruments):
                self.instruments = instruments
                return 75000.0

        fake_broker = MarginBroker()
        broker.set_broker_for_tests(fake_broker)
        route = SimpleNamespace(
            legs={"buy_pe": "BPE", "sell_pe": "SPE"},
            order_sequence=[("buy_pe", "BUY"), ("sell_pe", "SELL")],
        )

        result = position_sizing.calculate_position_size(
            "LIVE", "BTST_BULL_PUT_CREDIT", 16.0, 0.0, "NIFTY", 65, route=route
        )

        self.assertEqual(result["status"], "APPROVED")
        self.assertEqual(result["margin_source"], "UPSTOX_BASKET_MARGIN")
        self.assertEqual(result["lots_to_deploy"], 5)
        self.assertEqual(fake_broker.instruments[0]["quantity"], 65)

    def test_pcr_dte_matrix_routes_extreme_bullish_to_otm_bull_put(self):
        chain = self._option_chain_for_strikes({
            19000: (500.0, 5.0),
            19500: (300.0, 20.0),
            20000: (100.0, 100.0),
            20500: (20.0, 300.0),
        })
        for strike_data in chain:
            strike_data["call_options"]["market_data"]["oi"] = 100
            strike_data["put_options"]["market_data"]["oi"] = 140

        route = btst_vix_router.route_pcr_dte_strategy(
            "NIFTY",
            "2026-04-30",
            20000.0,
            chain,
            14.0,
            strategy.calculate_iron_butterfly_legs,
            now=datetime.datetime(2026, 4, 29, 10, 0),
        )

        self.assertEqual(route.strategy_type, btst_vix_router.STRATEGY_BTST_BULL_PUT_CREDIT)
        self.assertLess(route.strikes["sell_pe"], route.strikes["atm"])
        self.assertLess(route.strikes["buy_pe"], route.strikes["sell_pe"])

    def test_eod_call_side_slice_rewrites_state_to_put_credit_carry(self):
        config.ENVIRONMENT = "SANDBOX"
        state_manager.save_state(
            "NIFTY",
            {"buy_ce": "BCE", "buy_pe": "BPE", "sell_ce": "SCE", "sell_pe": "SPE"},
            {"buy_ce": 5.0, "buy_pe": 5.0, "sell_ce": 20.0, "sell_pe": 20.0},
            65,
            {"buy_ce": 150, "buy_pe": 50, "sell_ce": 100, "sell_pe": 100, "atm": 100},
        )
        state_manager.update_state("strategy_type", btst_vix_router.STRATEGY_IRON_CONDOR)

        self.assertTrue(execution.slice_neutral_side(
            "CALL",
            {"buy_ce": 6.0, "buy_pe": 4.0, "sell_ce": 25.0, "sell_pe": 18.0},
            exit_reason="EOD_SLICE_CALL_SIDE",
        ))
        state = state_manager.load_state()

        self.assertEqual(state["strategy_type"], btst_vix_router.STRATEGY_BTST_BULL_PUT_CREDIT)
        self.assertEqual(state["legs"], {"buy_pe": "BPE", "sell_pe": "SPE"})
        self.assertTrue(state["carry_overnight"])

    def test_btst_recenter_helper_attempts_exactly_one_fresh_entry(self):
        calls = []
        config.ENVIRONMENT = "SANDBOX"
        config.VIRTUAL_CAPITAL = 300000.0

        def fake_basket(legs, index_symbol, entry_prices, strikes, **kwargs):
            calls.append((legs, index_symbol, entry_prices, strikes, kwargs))
            state_manager.save_state(index_symbol, legs, entry_prices, kwargs.get("quantity", 65), strikes)
            return True

        with patch.object(main, "get_spot_price", return_value=100.0), \
             patch.object(main, "get_india_vix", return_value=10.0), \
             patch.object(main, "get_option_chain", return_value=[
                 {
                     "strike_price": 50,
                     "call_options": {"instrument_key": "C50", "market_data": {"ltp": 50, "oi": 100}, "greeks": {"delta": 0.95}},
                     "put_options": {"instrument_key": "P50", "market_data": {"ltp": 3, "oi": 90}, "greeks": {"delta": -0.05}},
                 },
                 {
                     "strike_price": 100,
                     "call_options": {"instrument_key": "C100", "market_data": {"ltp": 20, "oi": 100}, "greeks": {"delta": 0.50}},
                     "put_options": {"instrument_key": "P100", "market_data": {"ltp": 20, "oi": 90}, "greeks": {"delta": -0.50}},
                 },
                 {
                     "strike_price": 150,
                     "call_options": {"instrument_key": "C150", "market_data": {"ltp": 3, "oi": 100}, "greeks": {"delta": 0.05}},
                     "put_options": {"instrument_key": "P150", "market_data": {"ltp": 50, "oi": 90}, "greeks": {"delta": -0.95}},
                 },
             ]), \
             patch.object(main, "get_fresh_option_quotes", return_value={
                 "C100": 20.0,
                 "P100": 20.0,
                 "C150": 3.0,
                 "P50": 3.0,
             }), \
             patch.object(main, "place_iron_butterfly_basket", side_effect=fake_basket):
            self.assertTrue(main.deploy_single_sniper_trade("NIFTY", "2026-05-07", reason="BTST_RECENTER"))

        self.assertEqual(len(calls), 1)
        self.assertEqual(state_manager.load_state()["sniper_state"], "INITIAL")
        self.assertEqual(state_manager.load_state()["recenter_reason"], "BTST_RECENTER")


if __name__ == "__main__":
    unittest.main()
