import sys
import os
import requests
import pyotp
import json
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

import config

def test_auth():
    client_id = config.get_dhan_client_id()
    pin = config._setting("DHAN_PIN", "")
    totp_secret = config._setting("DHAN_TOTP_SECRET", "")
    
    print(f"Credentials loaded: Client ID={client_id}, PIN={pin}, TOTP Secret={'[SET]' if totp_secret else '[EMPTY]'}")
    if not client_id or not pin or not totp_secret:
        print("Missing credentials!")
        return

    clean_secret = str(totp_secret).replace(" ", "").strip()
    totp = pyotp.TOTP(clean_secret)
    current_totp = totp.now()
    
    payload = {
        "dhanClientId": str(client_id).strip(),
        "pin": str(pin).strip(),
        "totp": str(current_totp)
    }
    
    url = "https://auth.dhan.co/app/generateAccessToken"
    
    # 1. Test with Query Params (current implementation)
    print("\n--- Test 1: Query Parameters (params=payload) ---")
    try:
        r1 = requests.post(url, params=payload, headers={"Accept": "application/json"}, timeout=10)
        print("Status Code:", r1.status_code)
        print("Response Text:", r1.text)
    except Exception as e:
        print("Request failed:", e)

    # 2. Test with Form Data (data=payload)
    print("\n--- Test 2: Form Data (data=payload) ---")
    try:
        r2 = requests.post(url, data=payload, headers={"Accept": "application/json"}, timeout=10)
        print("Status Code:", r2.status_code)
        print("Response Text:", r2.text)
    except Exception as e:
        print("Request failed:", e)

    # 3. Test with JSON Body (json=payload)
    print("\n--- Test 3: JSON Body (json=payload) ---")
    try:
        r3 = requests.post(url, json=payload, headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=10)
        print("Status Code:", r3.status_code)
        print("Response Text:", r3.text)
    except Exception as e:
        print("Request failed:", e)

if __name__ == "__main__":
    test_auth()
