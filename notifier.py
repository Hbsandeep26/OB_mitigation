# notifier.py
import requests
import config
import logging

def send_telegram_alert(message):
    """Sends a formatted message to your Telegram app."""
    if not getattr(config, 'TELEGRAM_BOT_TOKEN', None) or not getattr(config, 'TELEGRAM_CHAT_ID', None):
        logging.warning("Telegram alert skipped because TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not configured.")
        return False
    
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML" # Allows us to use bold <b> and italic <i> tags
    }
    
    try:
        # We use a short timeout so a network glitch doesn't freeze your trading bot
        response = requests.post(url, json=payload, timeout=3)
        response.raise_for_status()
        return True
    except Exception as e:
        logging.error(f"Failed to send Telegram alert: {e}")
        return False
