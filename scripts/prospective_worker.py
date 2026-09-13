#!/usr/bin/env python3
"""Run the prospective evaluation scheduler outside the FastAPI process."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import db  # noqa: E402
from app.prospective.worker import process_once, run_forever  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()
    if args.once:
        print(f"processed={process_once()}")
    else:
        run_forever(args.interval)


if __name__ == "__main__":
    main()
