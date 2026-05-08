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
        self.old_targets_enabled = config.SNIPER_TARGETS_ENABLED
        self.old_drift_threshold = config.ATM_DRIFT_EJECT_THRESHOLD
        self.old_btst_recenter_min_drift = config.BTST_RECENTER_MIN_DRIFT_RATIO
        self.old_post_emergency_enabled = config.POST_EMERGENCY_REENTRY_ENABLED
        self.old_post_emergency_cooldown = config.POST_EMERGENCY_REENTRY_COOLDOWN_SECONDS
        self.old_catastrophe_multiplier = config.SNIPER_CATASTROPHE_MULTIPLIER
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
        config.BTST_RECENTER_MIN_DRIFT_RATIO = 0.06
        config.POST_EMERGENCY_REENTRY_ENABLED = True
        config.POST_EMERGENCY_REENTRY_COOLDOWN_SECONDS = 0.0
        config.SNIPER_CATASTROPHE_MULTIPLIER = 1.05
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
        config.BTST_RECENTER_MIN_DRIFT_RATIO = self.old_btst_recenter_min_drift
        config.POST_EMERGENCY_REENTRY_ENABLED = self.old_post_emergency_enabled
        config.POST_EMERGENCY_REENTRY_COOLDOWN_SECONDS = self.old_post_emergency_cooldown
        config.SNIPER_CATASTROPHE_MULTIPLIER = self.old_catastrophe_multiplier
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

    def test_btst_recenter_skips_when_drift_is_below_gate(self):
        self._save_sniper_state()
        btst_file = os.path.join(strategy.BASE_DIR, "btst_flag.txt")
        with open(btst_file, "w") as f:
            f.write("TRUE")
        live_data = self._sniper_live_data(20.0, 20.0, 5.0, 5.0, spot=102.0)

        with self._market_time_patch(15, 25):
            result, _ = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

        self.assertFalse(result)

    def test_btst_recenter_requires_minimum_atm_drift(self):
        self._save_sniper_state()
        btst_file = os.path.join(strategy.BASE_DIR, "btst_flag.txt")
        with open(btst_file, "w") as f:
            f.write("TRUE")
        live_data = self._sniper_live_data(18.0, 18.0, 4.5, 4.5, spot=106.0)

        with self._market_time_patch(15, 25):
            result, _ = strategy.risk_management_evaluator(live_data, state_manager.load_state()["legs"])

        self.assertEqual(result, "BTST_RECENTER")

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

    def test_btst_recenter_helper_attempts_exactly_one_fresh_entry(self):
        calls = []

        def fake_basket(legs, index_symbol, entry_prices, strikes, **kwargs):
            calls.append((legs, index_symbol, entry_prices, strikes, kwargs))
            state_manager.save_state(index_symbol, legs, entry_prices, 65, strikes)
            return True

        with patch.object(main, "get_spot_price", return_value=100.0), \
             patch.object(main, "get_option_chain", return_value=[
                 {
                     "strike_price": 50,
                     "call_options": {"instrument_key": "C50", "market_data": {"ltp": 50}, "greeks": {"delta": 0.95}},
                     "put_options": {"instrument_key": "P50", "market_data": {"ltp": 3}, "greeks": {"delta": -0.05}},
                 },
                 {
                     "strike_price": 100,
                     "call_options": {"instrument_key": "C100", "market_data": {"ltp": 20}, "greeks": {"delta": 0.50}},
                     "put_options": {"instrument_key": "P100", "market_data": {"ltp": 20}, "greeks": {"delta": -0.50}},
                 },
                 {
                     "strike_price": 150,
                     "call_options": {"instrument_key": "C150", "market_data": {"ltp": 3}, "greeks": {"delta": 0.05}},
                     "put_options": {"instrument_key": "P150", "market_data": {"ltp": 50}, "greeks": {"delta": -0.95}},
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
