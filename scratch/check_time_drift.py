import time
import requests
import datetime

def check_drift():
    # Get local system time
    local_now = datetime.datetime.now(datetime.timezone.utc)
    print(f"Local System Time (UTC): {local_now}")
    
    # Try fetching network time from WorldTimeAPI
    try:
        r = requests.get("http://worldtimeapi.org/api/timezone/Etc/UTC", timeout=5)
        if r.status_code == 200:
            net_time_str = r.json()["utc_datetime"]
            # Parse net_time_str e.g., "2026-06-21T18:08:45.123456+00:00"
            net_time = datetime.datetime.fromisoformat(net_time_str)
            diff = (net_time - local_now).total_seconds()
            print(f"Network Time (UTC)    : {net_time}")
            print(f"Time Drift            : {diff:.3f} seconds")
            return
    except Exception as e:
        print("Failed to get time from WorldTimeAPI:", e)
        
    # Fallback to HTTP Header Date from Dhan
    try:
        r = requests.head("https://auth.dhan.co", timeout=5)
        date_str = r.headers.get("Date")
        if date_str:
            # Parse RFC 5322 date e.g., "Sun, 21 Jun 2026 18:08:45 GMT"
            # Format: %a, %d %b %Y %H:%M:%S %Z
            net_time = datetime.datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=datetime.timezone.utc)
            diff = (net_time - local_now).total_seconds()
            print(f"Dhan Header Time (UTC): {net_time}")
            print(f"Time Drift            : {diff:.3f} seconds")
        else:
            print("No Date header found in response headers:", r.headers)
    except Exception as e:
        print("Failed to get time from Dhan header:", e)

if __name__ == "__main__":
    check_drift()
