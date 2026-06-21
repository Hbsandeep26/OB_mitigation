import json

def list_all_main_edits():
    path = r"C:\Users\sande\.gemini\antigravity\brain\ebbccbcd-b1ad-4bf3-bd43-e93344f05e0a\.system_generated\logs\transcript.jsonl"
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                text = str(data)
                if "main.py" in text and ("replace_file" in text or "write_to_file" in text or "write_file" in text or "ReplacementChunks" in text):
                    print(f"Step {data.get('step_index')}: {data.get('type')} - {data.get('tool_calls', [{}])[0].get('name') if data.get('tool_calls') else 'N/A'}")
            except Exception:
                pass

if __name__ == "__main__":
    list_all_main_edits()
