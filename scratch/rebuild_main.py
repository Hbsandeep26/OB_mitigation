import json
import re

def clean_original_code(file_path):
    code_lines = []
    # Matches original line number prefix, e.g. "779: def _credit_sweep_route"
    prefix_regex = re.compile(r"^(\d+):(.*)$")
    
    with open(file_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            if line_num <= 7:
                # Skip header lines
                continue
            line = line.rstrip("\r\n")
            m = prefix_regex.match(line)
            if m:
                code_part = m.group(2)
                # Strip at most one leading space
                if code_part.startswith(" "):
                    code_part = code_part[1:]
                code_lines.append(code_part)
            else:
                # If it's a blank line, keep it
                if not line.strip():
                    code_lines.append("")
                else:
                    code_lines.append(line)
                    
    return "\n".join(code_lines)

def apply_chunks(file_content, chunks, step_name):
    for idx, chunk in enumerate(chunks):
        target = chunk["TargetContent"]
        replacement = chunk["ReplacementContent"]
        
        # Standardize newlines to prevent formatting mismatches
        target_norm = target.replace("\r\n", "\n")
        replacement_norm = replacement.replace("\r\n", "\n")
        file_content_norm = file_content.replace("\r\n", "\n")
        
        if target_norm not in file_content_norm:
            print(f"Error: Target for {step_name} chunk {idx} not found in file content!")
            print("Looking for:\n", target_norm[:200])
            return None
            
        file_content_norm = file_content_norm.replace(target_norm, replacement_norm, 1)
        file_content = file_content_norm
        print(f"Successfully applied {step_name} chunk {idx}")
        
    return file_content

def main():
    # 1. Read clean main.py from HEAD
    with open("main.py", "r", encoding="utf-8") as f:
        clean_main = f.read().replace("\r\n", "\n")
        
    print(f"Clean main.py lines: {len(clean_main.splitlines())}")
    
    # 2. Extract and clean the Credit Sweep functions from step_482_content.txt
    cleaned_full = clean_original_code("scratch/step_482_content.txt")
    cleaned_lines = cleaned_full.splitlines()
    
    functions_code = []
    started = False
    for line in cleaned_lines:
        if line.strip().startswith("def _credit_sweep_route"):
            started = True
        if started:
            if line.strip().startswith("def scan_market_and_execute_trades"):
                break
            functions_code.append(line)
            
    functions_text = "\n".join(functions_code)
    print(f"Extracted Credit Sweep functions code: {len(functions_text.splitlines())} lines")
    
    # 3. Insert the functions into clean_main right before scan_market_and_execute_trades
    target_scan = "def scan_market_and_execute_trades():"
    if target_scan not in clean_main:
        print("Error: def scan_market_and_execute_trades(): not found in clean main.py!")
        return
        
    rebuilt_content = clean_main.replace(target_scan, functions_text + "\n\n" + target_scan, 1)
    print(f"Inserted functions. Rebuilt main.py lines: {len(rebuilt_content.splitlines())}")
    
    # 4. Apply Step 145 chunks
    with open("scratch/step_145.json", "r", encoding="utf-8") as f:
        step_145 = json.load(f)
    rebuilt_content = apply_chunks(rebuilt_content, step_145["ReplacementChunks"], "Step 145")
    if rebuilt_content is None:
        return
        
    # 5. Apply Step 541 chunks
    with open("scratch/step_541.json", "r", encoding="utf-8") as f:
        step_541 = json.load(f)
    rebuilt_content = apply_chunks(rebuilt_content, step_541["ReplacementChunks"], "Step 541")
    if rebuilt_content is None:
        return
        
    # 6. Apply token validation threshold change
    print("Applying token validation threshold change...")
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

    if target_token_check not in rebuilt_content:
        print("Error: Target for token validation check not found in rebuilt content!")
        return
        
    rebuilt_content = rebuilt_content.replace(target_token_check, replacement_token_check, 1)
    print("Successfully updated token validation threshold to 12 hours.")
    
    # 7. Write the rebuilt content back to main.py
    with open("main.py", "w", encoding="utf-8", newline="\n") as f:
        f.write(rebuilt_content)
        
    print(f"Rebuild completed successfully! main.py now has {len(rebuilt_content.splitlines())} lines.")

if __name__ == "__main__":
    main()
