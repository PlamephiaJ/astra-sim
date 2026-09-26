#!/usr/bin/env python3
"""exp2 entry point for the shared flow-aware Chakra DAG generator."""

from __future__ import annotations

import sys
from pathlib import Path


EXP_DIR = Path(__file__).resolve().parent.parent
COMMON_DIR = EXP_DIR.parent / "common"
sys.path.insert(0, str(COMMON_DIR))

from workload_generator import main  # noqa: E402


if __name__ == "__main__":
    main(EXP_DIR)
