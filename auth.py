# auth.py
import requests
import config
import logging

def get_daily_access_token(auth_code):
    """
    Exchanges the daily browser auth_code for a trading access_token.
    """
    url = 'https://api.upstox.com/v2/login/authorization/token'
    headers = {
        'accept': 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    data = {
        'code': auth_code,
        'client_id': config.API_KEY,
        'client_secret': config.API_SECRET,
        'redirect_uri': config.REDIRECT_URI,
        'grant_type': 'authorization_code',
    }

    try:
        response = requests.post(url, headers=headers, data=data)
        response.raise_for_status()
        token_data = response.json()
        access_token = token_data['access_token']
        logging.info("Successfully generated daily access token.")
        return access_token
    except Exception as e:
        logging.error(f"Failed to get access token: {e}")
        return None


def generate_dhan_token_with_totp(client_id, pin, totp_secret):
    """
    Generates a Dhan access token programmatically using Client ID, PIN, and TOTP Secret.
    """
    import pyotp
    url = "https://auth.dhan.co/app/generateAccessToken"
    try:
        # Strip spaces from TOTP secret key
        clean_secret = str(totp_secret).replace(" ", "").strip()
        totp = pyotp.TOTP(clean_secret)
        current_totp = totp.now()
        
        payload = {
            "dhanClientId": str(client_id).strip(),
            "pin": str(pin).strip(),
            "totp": str(current_totp)
        }
        headers = {
            "Accept": "application/json"
        }
        logging.info("Requesting fresh Dhan Access Token using TOTP...")
        response = requests.post(url, params=payload, headers=headers, timeout=10)
        
        if response.status_code != 200:
            logging.error("Dhan TOTP auth returned status %s: %s", response.status_code, response.text)
            return None
            
        data = response.json()
        token = data.get("accessToken") or data.get("data", {}).get("accessToken")
        if token:
            logging.info("Successfully generated Dhan access token via TOTP.")
            return token
            
        logging.error("Dhan TOTP login response missing token field: %s", data)
        return None
    except Exception as e:
        logging.error("Failed to generate Dhan access token via TOTP: %s", e)
        return None

