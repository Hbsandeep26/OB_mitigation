import json

def inspect():
    with open("scratch/step_145.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    print("Step 145 details:")
    print("Description:", data.get("Description"))
    print("Instruction:", data.get("Instruction"))
    chunks = data.get("ReplacementChunks", [])
    print(f"Total chunks: {len(chunks)}")
    for idx, chunk in enumerate(chunks):
        print(f"\n--- Chunk {idx} ---")
        print(f"StartLine: {chunk.get('StartLine')}, EndLine: {chunk.get('EndLine')}")
        target = chunk.get("TargetContent", "")
        repl = chunk.get("ReplacementContent", "")
        print(f"Target length: {len(target)}, Replacement length: {len(repl)}")
        # Check if _credit_sweep_route is in either
        if "_credit_sweep_route" in target:
            print("Found _credit_sweep_route in target!")
        if "_credit_sweep_route" in repl:
            print("Found _credit_sweep_route in replacement!")

if __name__ == "__main__":
    inspect()
