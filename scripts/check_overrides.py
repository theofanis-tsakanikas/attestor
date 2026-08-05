#!/usr/bin/env python3
"""Fail when an override has lapsed; warn when one is about to.

An expiry date that nothing enforces is a comment. This is what turns it into a mechanism:
CI goes red when an accepted defect outlives its acceptance, and nobody gets to sign one
permanently and quietly.

Two dates matter and they are not the same. Whether an override was *live* is judged as of
the report date, because that is the moment the report was issued and an auditor asks about
that moment. Whether it needs *re-reviewing* is judged as of today. Conflating them either
turns the repository permanently red the day after an expiry, or never red at all.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from attestor.contracts import overrides

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warn-days", type=int, default=30)
    parser.add_argument(
        "--today",
        type=dt.date.fromisoformat,
        default=dt.date.today(),
        help="Override the clock, so the check itself is testable.",
    )
    args = parser.parse_args()

    register = overrides.load_register(ROOT)
    if not len(register):
        print("no overrides on file")
        return 0

    expired = register.expired(args.today)
    expiring = [o for o in register.expiring_within(args.warn_days, args.today)]

    for override in expiring:
        remaining = (override.expires_on - args.today).days
        print(
            f"::warning::{override.tenant}/{override.datapoint_id} "
            f"({override.reason_code}) expires in {remaining} day(s) on {override.expires_on}"
        )

    for override in expired:
        print(
            f"::error::{override.tenant}/{override.datapoint_id} "
            f"({override.reason_code}) expired on {override.expires_on}. "
            "The defect it accepted now blocks the report again. Re-review it or fix the gap.",
            file=sys.stderr,
        )

    print(
        f"overrides: {len(register)} on file, {len(expiring)} expiring within "
        f"{args.warn_days} day(s), {len(expired)} expired"
    )
    return 1 if expired else 0


if __name__ == "__main__":
    raise SystemExit(main())
