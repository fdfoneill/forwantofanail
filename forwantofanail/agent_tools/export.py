from __future__ import annotations

import argparse
import json
from pathlib import Path

from .registry import catalog


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the provider-neutral commander tool catalog.")
    parser.add_argument("output", nargs="?", help="Output JSON file; stdout when omitted")
    args = parser.parse_args()
    rendered = json.dumps(catalog(), indent=2, sort_keys=False) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
