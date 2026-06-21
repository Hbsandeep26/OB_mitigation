import json

def extract_original():
    path = r"C:\Users\sande\.gemini\antigravity\brain\ebbccbcd-b1ad-4bf3-bd43-e93344f05e0a\.system_generated\logs\transcript_full.jsonl"
    
    # We will map step indices to the read contents
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
        print(f"\n=================== STEP {step_idx} VIEW CONTENT ===================")
        # print the first 1000 characters and last 1000 characters
        content = views[step_idx]
        lines = content.split("\n")
        print(f"Total lines returned: {len(lines)}")
        # Let's print the actual content of the lines that were shown
        for l in lines[:30]:
            print(l)
        print("...")
        for l in lines[-30:]:
            print(l)

if __name__ == "__main__":
    extract_original()
