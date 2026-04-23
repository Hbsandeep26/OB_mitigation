# execution.py
import config
import logging
import requests
import upstox_client
from upstox_client.rest import ApiException
from logger import log_trade
import state_manager
from notifier import send_telegram_alert
import time  # --- Added for rate limiting ---

def place_iron_butterfly_basket(legs, index_symbol, entry_prices, strikes):
    trade_quantity = config.get_nifty_qty() if index_symbol == "NIFTY" else config.get_sensex_qty()
    
    # ---------------------------------------------------------
    # 1. LOCAL PAPER TRADING (Bypassing the broken Upstox Sandbox)
    # ---------------------------------------------------------
    if config.ENVIRONMENT == "SANDBOX":
        logging.info("🟡 SANDBOX MODE DETECTED: Simulating local paper execution...")
        
        # We pretend the orders executed perfectly at the target entry prices
        net_premium = (entry_prices['sell_ce'] + entry_prices['sell_pe']) - (entry_prices['buy_ce'] + entry_prices['buy_pe'])
        
        logging.info("✅ Simulated Basket Executed Successfully!")
        log_trade("ENTRY", index_symbol, entry_prices, net_premium, 0.0, "Local Paper Trade (Simulated)")
        state_manager.save_state(index_symbol, legs, entry_prices, trade_quantity, strikes)
        
        return True 

    # ---------------------------------------------------------
    # 2. LIVE EXECUTION (Using Real Money)
    # ---------------------------------------------------------
    elif config.ENVIRONMENT == "LIVE":
        logging.critical("🔴 LIVE MODE DETECTED: Routing orders to real Upstox Exchange...")
        
        configuration = upstox_client.Configuration()
        configuration.access_token = config.get_live_token()
        
        api_client = upstox_client.ApiClient(configuration)
        api_instance = upstox_client.OrderApiV3(api_client)
        
        orders = [
            {"token": legs['buy_ce'], "type": "BUY"},
            {"token": legs['buy_pe'], "type": "BUY"},
            {"token": legs['sell_ce'], "type": "SELL"},
            {"token": legs['sell_pe'], "type": "SELL"}
        ]
        
        success = True
        filled_legs = [] # --- THE AUTO-ROLLBACK FIX: Track success ---
        
        for order in orders:
            body = upstox_client.PlaceOrderV3Request(
                quantity=int(trade_quantity),
                product="I", 
                validity="DAY",
                price=0.0,
                tag="iron_fly",
                instrument_token=order["token"],
                order_type="MARKET",
                transaction_type=order["type"],
                disclosed_quantity=0,
                trigger_price=0.0,
                is_amo=False,
                slice=False 
            )
            
            try:
                api_instance.place_order(body)
                logging.info(f"Live Order Placed: {order['token']} - Status: success")
                filled_legs.append(order)
                time.sleep(0.15) # Protect against entry rate limits
            except ApiException as e:
                logging.error(f"Live Order Rejected for {order['token']}: {e.body}")
                success = False
                break # Stop placing further orders
            except Exception as e:
                logging.error(f"System Error placing Live Order for {order['token']}: {e}")
                success = False
                break
                
        # --- THE AUTO-ROLLBACK FIX: Clean up the mess ---
        if not success and len(filled_legs) > 0:
            logging.critical("🛑 BASKET FAILED MID-EXECUTION! Initiating emergency rollback of filled legs...")
            for filled_order in filled_legs:
                reverse_type = "SELL" if filled_order["type"] == "BUY" else "BUY"
                rollback_body = upstox_client.PlaceOrderV3Request(
                    quantity=int(trade_quantity), product="I", validity="DAY", price=0.0,
                    tag="iron_fly_rollback", instrument_token=filled_order["token"],
                    order_type="MARKET", transaction_type=reverse_type, disclosed_quantity=0,
                    trigger_price=0.0, is_amo=False, slice=False
                )
                try:
                    api_instance.place_order(rollback_body)
                    logging.info(f"Rollback successful for {filled_order['token']}")
                    time.sleep(0.15)
                except Exception as rollback_err:
                    logging.critical(f"FATAL: Rollback failed for {filled_order['token']}: {rollback_err}")
            
            return False 
                
        if success:
            net_premium = (entry_prices['sell_ce'] + entry_prices['sell_pe']) - (entry_prices['buy_ce'] + entry_prices['buy_pe'])
            log_trade("ENTRY", index_symbol, entry_prices, net_premium, 0.0, "Live Basket Executed")
            state_manager.save_state(index_symbol, legs, entry_prices, trade_quantity, strikes)
       
            alert_msg = (
                f"🟢 <b>TRADE DEPLOYED: {index_symbol}</b>\n"
                f"Net Premium Collected: ₹{net_premium:.2f}\n"
                f"Quantity: {trade_quantity}"
            )   
            send_telegram_alert(alert_msg)

        return success


def square_off_all(exit_prices=None):
    logging.critical("TRIGGERING SQUARE OFF SEQUENCE!")
    success = True
    state = state_manager.load_state()

    if state:
        entry = state['entry_prices']
        qty = state.get('quantity', 0)
        legs = state['legs']
        index_symbol = state['index_symbol']

        # --- THE BLACKOUT FIX: Safe Fallback Dictionary ---
        if not exit_prices:
            exit_prices = {}
            
        safe_exit_sell_ce = exit_prices.get('sell_ce', entry['sell_ce'])
        safe_exit_sell_pe = exit_prices.get('sell_pe', entry['sell_pe'])
        safe_exit_buy_ce = exit_prices.get('buy_ce', entry['buy_ce'])
        safe_exit_buy_pe = exit_prices.get('buy_pe', entry['buy_pe'])

        # Premium Paid to close the trade
        exit_premium = (safe_exit_sell_ce + safe_exit_sell_pe) - (safe_exit_buy_ce + safe_exit_buy_pe)

        # Real PnL Calculation
        pnl = (entry['sell_ce'] + entry['sell_pe'] - safe_exit_sell_ce - safe_exit_sell_pe) * qty
        pnl += (safe_exit_buy_ce + safe_exit_buy_pe - entry['buy_ce'] - entry['buy_pe']) * qty

        success = True

        if config.ENVIRONMENT == "SANDBOX":
            log_trade("EXIT", index_symbol, exit_prices, exit_premium, pnl, "Local Paper Trade Closed")
            logging.info(f"💰 Simulated PnL for this trade: ₹{pnl:.2f}")

            status_icon = "🤑" if pnl > 0 else "🩸"
            alert_msg = (
                f"🔴 <b>TRADE CLOSED (PAPER): {index_symbol}</b>\n"
                f"{status_icon} Realized PnL: <b>₹{pnl:.2f}</b>\n"
                f"Reason: <i>Risk/Target limits hit</i>"
            )
            send_telegram_alert(alert_msg)

        elif config.ENVIRONMENT == "LIVE":
            logging.critical("🔴 ROUTING EXIT ORDERS TO LIVE UPSTOX EXCHANGE...")

            configuration = upstox_client.Configuration()
            configuration.access_token = config.get_live_token()

            api_client = upstox_client.ApiClient(configuration)
            api_instance = upstox_client.OrderApiV3(api_client)

            # Helper function for clean order building
            def build_order_request(token, tx_type):
                return upstox_client.PlaceOrderV3Request(
                    quantity=int(qty), product="I", validity="DAY", price=0.0,
                    tag="iron_fly_exit", instrument_token=token, order_type="MARKET",
                    transaction_type=tx_type, disclosed_quantity=0, trigger_price=0.0,
                    is_amo=False, slice=False
                )

            success = True
            
            # --- THE NAKED EXIT FIX: Paired Execution ---
            
            # Pair 1: Call Side
            try:
                # 1. Close Short CE
                api_instance.place_order(build_order_request(legs['sell_ce'], "BUY"))
                logging.info("Live Exit: Short CE closed successfully.")
                time.sleep(0.15) # THE RATE LIMIT FIX
                
                # 2. Close Long CE
                api_instance.place_order(build_order_request(legs['buy_ce'], "SELL"))
                logging.info("Live Exit: Long CE closed successfully.")
                time.sleep(0.15)
            except Exception as e:
                logging.critical(f"🛑 FAILED TO CLOSE CALL SIDE! Keeping hedge active to prevent naked risk. Error: {e}")
                success = False

            # Pair 2: Put Side
            try:
                # 1. Close Short PE
                api_instance.place_order(build_order_request(legs['sell_pe'], "BUY"))
                logging.info("Live Exit: Short PE closed successfully.")
                time.sleep(0.15)
                
                # 2. Close Long PE
                api_instance.place_order(build_order_request(legs['buy_pe'], "SELL"))
                logging.info("Live Exit: Long PE closed successfully.")
                time.sleep(0.15)
            except Exception as e:
                logging.critical(f"🛑 FAILED TO CLOSE PUT SIDE! Keeping hedge active to prevent naked risk. Error: {e}")
                success = False

            log_trade("EXIT", index_symbol, exit_prices, exit_premium, pnl, "Live Exchange Exit")

            # --- THE TELEGRAM PROFIT POLISH (LIVE) ---
            status_icon = "🤑" if pnl > 0 else "🩸"
            alert_msg = (
                f"🔴 <b>TRADE CLOSED (LIVE): {index_symbol}</b>\n"
                f"{status_icon} Realized PnL: <b>₹{pnl:.2f}</b>\n"
                f"Net Premium Exited: ₹{exit_premium:.2f}\n"
                f"Status: {'✅ Execution Safe' if success else '⚠️ WARNING: Leg Failure!'}"
            )
            send_telegram_alert(alert_msg)

    else:
        log_trade("EXIT", "UNKNOWN", {}, 0.0, 0.0, "Emergency Square Off")
        send_telegram_alert("⚠️ <b>EMERGENCY SQUARE OFF TRIGGERED!</b> Check AWS Terminal immediately.")

    if success:
        state_manager.clear_state()
    else:
        logging.critical("🛑 STATE RETAINED: Exit execution failed! Trade state preserved in memory for manual recovery.")
