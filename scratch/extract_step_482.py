import json

def extract_step_482():
    path = r"C:\Users\sande\.gemini\antigravity\brain\ebbccbcd-b1ad-4bf3-bd43-e93344f05e0a\.system_generated\logs\transcript_full.jsonl"
    
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                step_idx = data.get("step_index")
                if step_idx == 482:
                    out_file = "c:\\Users\\sande\\Antigravity_upstox_selling\\Dhan_Algo\\scratch\\step_482_content.txt"
                    with open(out_file, "w", encoding="utf-8") as out:
                        out.write(data.get("content", ""))
                    print(f"Saved Step 482 content to {out_file}")
                    return
            except Exception as e:
                print("Error:", e)

if __name__ == "__main__":
    extract_step_482()
