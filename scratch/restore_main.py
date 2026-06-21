import json
import re

def apply_chunks(file_content, chunks):
    for i, chunk in enumerate(chunks):
        target = chunk["TargetContent"]
        replacement = chunk["ReplacementContent"]
        
        # Check if target is in file_content
        if target not in file_content:
            print(f"Error: Target for chunk {i} not found in file content!")
            # Let's print a preview of target to help debug
            print("Target preview:", target[:100])
            return None
            
        file_content = file_content.replace(target, replacement, 1)
        print(f"Successfully applied chunk {i}")
        
    return file_content

def main():
    # Read original main.py
    with open("main.py", "r", encoding="utf-8") as f:
        content = f.read()
        
    print(f"Original main.py length: {len(content)}")
    
    # 1. Load and apply Step 145 chunks
    with open("scratch/step_145.json", "r", encoding="utf-8") as f:
        step_145 = json.load(f)
    print("\nApplying Step 145 chunks...")
    content = apply_chunks(content, step_145["ReplacementChunks"])
    if content is None:
        return
        
    # 2. Load and apply Step 541 chunks
    with open("scratch/step_541.json", "r", encoding="utf-8") as f:
        step_541 = json.load(f)
    print("\nApplying Step 541 chunks...")
    content = apply_chunks(content, step_541["ReplacementChunks"])
    if content is None:
        return
        
    # 3. Apply the token validation threshold change (1800 -> 43200)
    print("\nApplying token validation threshold change...")
    target_token_check = """        exp = payload.get("exp")
        if exp:
            return float(exp) > time.time() + 1800
    except Exception:
        pass
    return False"""

    replacement_token_check = """        exp = payload.get("exp")
        if exp:
            return float(exp) > time.time() + 43200
    except Exception:
        pass
    return False"""

    if target_token_check not in content:
        print("Error: Target for token validation check not found in content!")
        return
        
    content = content.replace(target_token_check, replacement_token_check, 1)
    print("Successfully updated token validation threshold to 12 hours.")
    
    # Save the restored main.py
    with open("main.py", "w", encoding="utf-8") as f:
        f.write(content)
        
    print(f"\nRestoration complete! Saved main.py (length: {len(content)})")

if __name__ == "__main__":
    main()
