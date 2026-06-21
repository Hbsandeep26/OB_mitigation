import json

def find_subagents():
    path = r"C:\Users\sande\.gemini\antigravity\brain\ebbccbcd-b1ad-4bf3-bd43-e93344f05e0a\.system_generated\logs\transcript_full.jsonl"
    
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                step_idx = data.get("step_index")
                tool_calls = data.get("tool_calls", [])
                for tc in tool_calls:
                    if "subagent" in tc.get("name", ""):
                        print(f"Step {step_idx}: Tool={tc.get('name')}, Prompt={tc.get('args', {}).get('Prompt') or tc.get('args', {}).get('Prompt')}")
            except Exception:
                pass

if __name__ == "__main__":
    find_subagents()
