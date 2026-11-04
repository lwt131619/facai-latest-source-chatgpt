"""Measure registered artifact duplication without changing files or database."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from facai.artifact_inventory import inventory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("FACAI_DATA_DIR", ROOT / "data")))
    args = parser.parse_args()
    print(json.dumps(inventory(args.data_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
