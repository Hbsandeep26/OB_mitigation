import json

def list_views():
    path = r"C:\Users\sande\.gemini\antigravity\brain\ebbccbcd-b1ad-4bf3-bd43-e93344f05e0a\.system_generated\logs\transcript_full.jsonl"
    
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                step_idx = data.get("step_index")
                tool_calls = data.get("tool_calls", [])
                for tc in tool_calls:
                    if tc.get("name") == "view_file" and "main.py" in str(tc.get("args")):
                        print(f"Step {step_idx}: args={tc.get('args')}")
            except Exception:
                pass

if __name__ == "__main__":
    list_views()
