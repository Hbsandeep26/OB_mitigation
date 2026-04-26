# state_manager.py
import json
import os
import tempfile
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "trade_state.json")

# In-memory cache to prevent disk I/O bottlenecks during live websocket streaming
_cached_state = None
_last_mtime = 0.0

def _atomic_write(filepath, data):
    """Writes JSON data to a temporary file and atomically replaces the target file."""
    temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(filepath))
    with os.fdopen(temp_fd, 'w') as f:
        json.dump(data, f, indent=4)
    os.replace(temp_path, filepath)

def save_state(index_symbol, legs, entry_prices, quantity, strikes=None):
    global _cached_state, _last_mtime
    existing_state = load_state() or {}

    state = {
        "active": True,
        "index_symbol": index_symbol,
        "legs": legs,
        "entry_prices": entry_prices,
        "quantity": quantity,
        "strikes": strikes or existing_state.get("strikes", {})
    }
    
    _atomic_write(STATE_FILE, state)
    
    # Update cache immediately to avoid re-reading
    _cached_state = state
    _last_mtime = time.time() # Approximation, next read will sync with actual mtime

def load_state():
    global _cached_state, _last_mtime
    if not os.path.exists(STATE_FILE):
        return None
        
    try:
        current_mtime = os.path.getmtime(STATE_FILE)
        # If file was modified since last read, or cache is empty, read from disk
        if current_mtime > _last_mtime or _cached_state is None:
            with open(STATE_FILE, "r") as f:
                _cached_state = json.load(f)
            _last_mtime = current_mtime
    except Exception:
        pass
        
    return _cached_state

def clear_state():
    global _cached_state, _last_mtime
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    _cached_state = None
    _last_mtime = 0.0

def update_state(key, value):
    """Dynamically injects or updates a specific key in the active trade state."""
    global _cached_state, _last_mtime
    
    state = load_state()
    if state is not None:
        state[key] = value
        try:
            _atomic_write(STATE_FILE, state)
            _cached_state = state
            _last_mtime = time.time()
        except Exception:
            pass
