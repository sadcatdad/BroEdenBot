#!/usr/bin/env python3
"""Manual private-library sync and status. Run from the repository root."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from utils.notion_library import sync_library

if __name__ == "__main__":
    load_dotenv()
    result = sync_library()
    print(result)
    raise SystemExit(1 if result.get("failed") else 0)
