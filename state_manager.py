# state_manager.py
import json
import os

#STATE_FILE = "trade_state.json"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "trade_state.json")

def save_state(index_symbol, legs, entry_prices, quantity, strikes=None):
    # Safely load existing state just to preserve strikes if updating an open trade
    existing_state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                existing_state = json.load(f)
        except Exception:
            pass

    state = {
        "active": True,
        "index_symbol": index_symbol,
        "legs": legs,
        "entry_prices": entry_prices,
        "quantity": quantity,
        "strikes": strikes or existing_state.get("strikes", {})
    }
    
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return None
    return None

def clear_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

def update_state(key, value):
    """Dynamically injects or updates a specific key in the active trade state."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            state[key] = value
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=4)
        except Exception:
            pass        
