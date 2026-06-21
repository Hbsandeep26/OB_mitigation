import json

def inspect_steps():
    path = r"C:\Users\sande\.gemini\antigravity\brain\ebbccbcd-b1ad-4bf3-bd43-e93344f05e0a\.system_generated\logs\transcript_full.jsonl"
    
    # We want to find step 7, 8, 9, 10... and print their fields to understand where the output is.
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                step_idx = data.get("step_index")
                if step_idx in [7, 8, 9, 10]:
                    print(f"\n--- Step {step_idx} (Type: {data.get('type')}, Source: {data.get('source')}) ---")
                    print("Keys:", list(data.keys()))
                    # If there's content, print its length and first 200 chars
                    if "content" in data:
                        print(f"Content length: {len(data['content'])}")
                        print("Content preview:", data["content"][:200].replace("\n", " "))
                    # Check for other fields like 'output' or 'results'
                    for key in ["output", "result", "tool_output", "error"]:
                        if key in data:
                            print(f"{key} length: {len(str(data[key]))}")
                            print(f"{key} preview:", str(data[key])[:200].replace("\n", " "))
            except Exception as e:
                print("Error:", e)

if __name__ == "__main__":
    inspect_steps()
