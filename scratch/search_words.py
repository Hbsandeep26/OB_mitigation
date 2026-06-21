def search_words():
    for step in [8, 10, 14]:
        file = f"scratch/original_step_{step}.txt"
        with open(file, "r", encoding="utf-8") as f:
            content = f.read()
        print(f"File {file}:")
        for word in ["credit", "sweep"]:
            count = content.lower().count(word)
            print(f"  Word '{word}' count: {count}")

if __name__ == "__main__":
    search_words()
