import json

def get_steps():
    path = r"C:\Users\sande\.gemini\antigravity\brain\ebbccbcd-b1ad-4bf3-bd43-e93344f05e0a\.system_generated\logs\transcript_full.jsonl"
    target_steps = [145, 541]
    
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                step_idx = data.get("step_index")
                if step_idx in target_steps:
                    print(f"\n=================== STEP {step_idx} ===================")
                    tool_calls = data.get("tool_calls", [])
                    for tc in tool_calls:
                        if "main.py" in str(tc):
                            # Pretty print the tool call arguments
                            print(json.dumps(tc.get("args"), indent=2))
            except Exception as e:
                print("Error parsing line:", e)

if __name__ == "__main__":
    get_steps()
