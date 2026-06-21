import json

def save_steps():
    path = r"C:\Users\sande\.gemini\antigravity\brain\ebbccbcd-b1ad-4bf3-bd43-e93344f05e0a\.system_generated\logs\transcript_full.jsonl"
    
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                step_idx = data.get("step_index")
                if step_idx in (145, 541):
                    tool_calls = data.get("tool_calls", [])
                    for tc in tool_calls:
                        if "main.py" in str(tc):
                            out_file = f"c:\\Users\\sande\\Antigravity_upstox_selling\\Dhan_Algo\\scratch\\step_{step_idx}.json"
                            with open(out_file, "w", encoding="utf-8") as out:
                                json.dump(tc.get("args"), out, indent=2)
                            print(f"Saved Step {step_idx} to {out_file}")
            except Exception as e:
                print("Error:", e)

if __name__ == "__main__":
    save_steps()
