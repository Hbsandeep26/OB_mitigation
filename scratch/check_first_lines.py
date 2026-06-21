import json

def check_first_lines():
    path = r"C:\Users\sande\.gemini\antigravity\brain\ebbccbcd-b1ad-4bf3-bd43-e93344f05e0a\.system_generated\logs\transcript.jsonl"
    with open(path, "r", encoding="utf-8") as f:
        for i in range(5):
            line = f.readline()
            if not line:
                break
            try:
                data = json.loads(line)
                print(f"Line {i}: Step {data.get('step_index')}, Type: {data.get('type')}, Source: {data.get('source')}")
            except Exception as e:
                print("Error:", e)

if __name__ == "__main__":
    check_first_lines()
