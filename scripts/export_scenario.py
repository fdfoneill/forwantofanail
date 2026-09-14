#!/usr/bin/env python3
"""Repository entry point for versioned scenario-package archives."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forwantofanail.core.scenario_archive import main


if __name__ == "__main__":
    main()
