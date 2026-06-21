import sys
import logging
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

import main

# Setup logger to see output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def run_test():
    print("Starting auto_authenticate_dhan execution...")
    main.auto_authenticate_dhan()
    print("Execution finished.")

if __name__ == "__main__":
    run_test()
