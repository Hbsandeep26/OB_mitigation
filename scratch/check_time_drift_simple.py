import datetime
import requests
import time

def check():
    local_now = datetime.datetime.now(datetime.timezone.utc)
    print("Local UTC:", local_now)
    try:
        t0 = time.time()
        r = requests.head("https://www.google.com", timeout=5)
        t1 = time.time()
        latency = t1 - t0
        date_str = r.headers.get("Date")
        if date_str:
            net_time = datetime.datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=datetime.timezone.utc)
            diff = (net_time - local_now).total_seconds()
            print("Google UTC:", net_time)
            print("Latency   :", latency)
            print("Drift     :", diff)
        else:
            print("No Date header found")
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    check()
