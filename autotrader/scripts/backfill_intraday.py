#!/usr/bin/env python
"""Thin shim: build the intraday daytrade snapshot library.

    python autotrader/scripts/backfill_intraday.py [--days 60] [--tape-only]

The real logic lives in autotrader.intraday_data (importable by the daemon).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from autotrader.intraday_data import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
