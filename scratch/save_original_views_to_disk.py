import json

def save_views():
    path = r"C:\Users\sande\.gemini\antigravity\brain\ebbccbcd-b1ad-4bf3-bd43-e93344f05e0a\.system_generated\logs\transcript_full.jsonl"
    
    views = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                step_idx = data.get("step_index")
                if step_idx in [8, 10, 14]:
                    views[step_idx] = data.get("content", "")
            except Exception:
                pass
                
    for step_idx in sorted(views.keys()):
        out_file = f"c:\\Users\\sande\\Antigravity_upstox_selling\\Dhan_Algo\\scratch\\original_step_{step_idx}.txt"
        with open(out_file, "w", encoding="utf-8") as out:
            out.write(views[step_idx])
        print(f"Saved Step {step_idx} view to {out_file}")

if __name__ == "__main__":
    save_views()
