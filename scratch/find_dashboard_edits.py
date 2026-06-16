import json
import os

transcript_path = r"C:\Users\sande\.gemini\antigravity\brain\2b8fa79b-e68d-4afd-ab3d-66afed5e6483\.system_generated\logs\transcript.jsonl"
output_path = "scratch/dashboard_edits_found.txt"

with open(transcript_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

edits = []
for i, line in enumerate(lines):
    try:
        obj = json.loads(line)
        if obj.get('type') == 'PLANNER_RESPONSE':
            tool_calls = obj.get('tool_calls', [])
            for tc in tool_calls:
                name = tc.get('name')
                args = tc.get('args', {})
                # Check if this tool call was writing/modifying dashboard.py
                if name in ('replace_file_content', 'multi_replace_file_content', 'write_to_file'):
                    target = args.get('TargetFile', '') or args.get('TargetFile', '')
                    if 'dashboard.py' in str(target) or 'dashboard.py' in str(args):
                        edits.append({
                            "line_number": i,
                            "tool_name": name,
                            "args": args
                        })
    except Exception as e:
        pass

with open(output_path, "w", encoding="utf-8") as out:
    for edit in edits:
        out.write(f"=== LINE {edit['line_number']}: {edit['tool_name']} ===\n")
        out.write(json.dumps(edit['args'], indent=2))
        out.write("\n\n")

print(f"Done! Found {len(edits)} edits. Output written to {output_path}")
