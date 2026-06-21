import json

def trace_writes():
    path = r"C:\Users\sande\.gemini\antigravity\brain\ebbccbcd-b1ad-4bf3-bd43-e93344f05e0a\.system_generated\logs\transcript_full.jsonl"
    
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                step_idx = data.get("step_index")
                tool_calls = data.get("tool_calls", [])
                for tc in tool_calls:
                    tc_str = str(tc)
                    if "main.py" in tc_str and any(w in tc.get("name", "") for w in ["write", "replace", "edit", "create", "modify"]):
                        print(f"Step {step_idx}: Tool={tc.get('name')}, Args keys={list(tc.get('args', {}).keys())}")
                        if "Description" in tc.get("args", {}):
                            print(f"  Description: {tc['args']['Description']}")
                        if "Instruction" in tc.get("args", {}):
                            print(f"  Instruction: {tc['args']['Instruction']}")
            except Exception as e:
                pass

if __name__ == "__main__":
    trace_writes()
