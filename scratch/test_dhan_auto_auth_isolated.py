import sys
import logging
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

import auth
import config

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def test():
    client_id = config.get_dhan_client_id()
    pin = config._setting("DHAN_PIN", "")
    totp_secret = config._setting("DHAN_TOTP_SECRET", "")
    
    print("\n--- Running Isolated Dhan Token Generation Test ---")
    print(f"Client ID: {client_id}")
    print(f"PIN: {pin}")
    print(f"TOTP Secret: {'[LOADED]' if totp_secret else '[EMPTY]'}")
    
    token = auth.generate_dhan_token_with_totp(client_id, pin, totp_secret)
    print(f"Returned Token: {token}")
    print("--------------------------------------------------\n")

if __name__ == "__main__":
    test()
