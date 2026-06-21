import sys
import os
import time
import pyotp
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

import config

def show_totp():
    totp_secret = config._setting("DHAN_TOTP_SECRET", "")
    if not totp_secret:
        print("Error: DHAN_TOTP_SECRET is empty or not found in settings.json.")
        return
        
    clean_secret = str(totp_secret).replace(" ", "").strip()
    try:
        totp = pyotp.TOTP(clean_secret)
        current_totp = totp.now()
        
        # Calculate seconds remaining in the current 30s window
        time_remaining = int(30 - (time.time() % 30))
        
        print("\n================ DHAN TOTP GENERATOR ================")
        print(f"Configured Secret Key (Cleaned): {clean_secret}")
        print(f"Generated TOTP Code           : {current_totp}")
        print(f"Time Remaining in current step : {time_remaining} seconds")
        print("=====================================================")
        print("Please check if the Generated TOTP Code matches the code")
        print("shown in your Google Authenticator / Dhan app.")
        print("If it does not match, your DHAN_TOTP_SECRET is incorrect.")
    except Exception as e:
        print("Failed to generate TOTP:", e)

if __name__ == "__main__":
    show_totp()
