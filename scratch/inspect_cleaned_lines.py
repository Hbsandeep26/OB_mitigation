import re

def inspect_cleaning():
    file_path = "scratch/step_482_content.txt"
    prefix_regex = re.compile(r"^\d+:\s+\d+:(.*)$")
    
    with open(file_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.rstrip("\r\n")
            m = prefix_regex.match(line)
            if m:
                code_part = m.group(1)
                # print prefix and cleaned result
                print(f"Line {line_num} matched! Raw: {line[:50]} -> Cleaned: {code_part[:50]}")
            else:
                print(f"Line {line_num} DID NOT MATCH! Raw: {line[:50]}")
            if line_num > 25:
                break

if __name__ == "__main__":
    inspect_cleaning()
